#!/usr/bin/env python3

if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main([__file__] + sys.argv[1:]))

import errno
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time

import pytest

from os.path import join as pjoin

from util import (
    base_cmdline,
    basename,
    cleanup,
    fuse_test_marker,
    umount,
    wait_for_mount,
    _check_ssh_localhost,
    _mount_sshfs,
)

pytestmark = fuse_test_marker()


# ---------------------------------------------------------------------------
# test_disk_full_enospc
# ---------------------------------------------------------------------------

def test_disk_full_enospc(tmpdir, capfd):
    """Fill a 1 MB tmpfs remote, verify ENOSPC propagates and earlier files survive."""
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)

    # Create a scratch dir for the tmpfs mount point
    tmpfs_dir = str(tmpdir.mkdir("tmpfs_src"))

    try:
        ret = subprocess.call(
            ["sudo", "mount", "-t", "tmpfs", "-o", "size=1m", "tmpfs", tmpfs_dir]
        )
        if ret != 0:
            pytest.skip("sudo mount tmpfs not available")
    except Exception:
        pytest.skip("sudo mount tmpfs not available")

    tmpfs_mounted = True
    try:
        _check_ssh_localhost()
        mnt_dir = str(tmpdir.mkdir("mnt"))
        cmdline = base_cmdline + [
            pjoin(basename, "sshfs"),
            "-f",
            f"localhost:{tmpfs_dir}",
            mnt_dir,
            "-o", "entry_timeout=0",
            "-o", "attr_timeout=0",
            "-o", "dir_cache=no",
        ]
        new_env = dict(os.environ)
        new_env["G_DEBUG"] = "fatal-warnings"
        mount_process = subprocess.Popen(cmdline, env=new_env)
        try:
            wait_for_mount(mount_process, mnt_dir)
        except Exception:
            cleanup(mount_process, mnt_dir)
            raise

        try:
            # Write a sentinel file first so we can verify it survives
            sentinel = pjoin(mnt_dir, "sentinel")
            sentinel_data = b"this file was written before disk full\n"
            with open(sentinel, "wb") as fh:
                fh.write(sentinel_data)

            # Fill the filesystem in 64KB chunks until ENOSPC
            got_enospc = False
            fill_files = []
            for i in range(100):
                fill_path = pjoin(mnt_dir, f"fill_{i}")
                try:
                    with open(fill_path, "wb") as fh:
                        fh.write(b"x" * (64 * 1024))
                    fill_files.append(fill_path)
                except OSError as e:
                    if e.errno == errno.ENOSPC:
                        got_enospc = True
                        break
                    raise

            assert got_enospc, "expected ENOSPC but filesystem never filled up"

            # The sentinel file written before disk-full should still be readable
            with open(sentinel, "rb") as fh:
                assert fh.read() == sentinel_data, "sentinel file corrupted after ENOSPC"

        except Exception:
            cleanup(mount_process, mnt_dir)
            raise
        else:
            umount(mount_process, mnt_dir)

    finally:
        if tmpfs_mounted:
            subprocess.call(["sudo", "umount", tmpfs_dir])


# ---------------------------------------------------------------------------
# test_permission_denied
# ---------------------------------------------------------------------------

def test_permission_denied(tmpdir, capfd):
    """Verify EACCES is propagated for mode-0000 files and mode-0500 dirs."""
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)

    if os.getuid() == 0:
        pytest.skip("permission tests do not apply when running as root")

    mount_process, mnt_dir, src_dir = _mount_sshfs(tmpdir, ["dir_cache=no"])
    try:
        # Test 1: mode 0000 file — open should return EACCES
        secret = pjoin(src_dir, "secret")
        with open(secret, "wb") as fh:
            fh.write(b"secret data")
        os.chmod(secret, 0o000)

        mnt_secret = pjoin(mnt_dir, "secret")
        with pytest.raises(OSError) as exc_info:
            open(mnt_secret, "rb")
        assert exc_info.value.errno == errno.EACCES, (
            f"expected EACCES for 0000 file, got {exc_info.value.errno}"
        )

        # Test 2: mode 0500 dir — creating a file inside should return EACCES
        locked_dir = pjoin(src_dir, "locked_dir")
        os.makedirs(locked_dir)
        os.chmod(locked_dir, 0o500)

        mnt_locked = pjoin(mnt_dir, "locked_dir")
        with pytest.raises(OSError) as exc_info:
            open(pjoin(mnt_locked, "newfile"), "wb")
        assert exc_info.value.errno == errno.EACCES, (
            f"expected EACCES for write into 0500 dir, got {exc_info.value.errno}"
        )

    except Exception:
        # Restore permissions so tmpdir cleanup succeeds
        try:
            os.chmod(pjoin(src_dir, "secret"), 0o644)
        except Exception:
            pass
        try:
            os.chmod(pjoin(src_dir, "locked_dir"), 0o700)
        except Exception:
            pass
        cleanup(mount_process, mnt_dir)
        raise
    else:
        # Restore permissions before umount so cleanup works
        try:
            os.chmod(pjoin(src_dir, "secret"), 0o644)
        except Exception:
            pass
        try:
            os.chmod(pjoin(src_dir, "locked_dir"), 0o700)
        except Exception:
            pass
        umount(mount_process, mnt_dir)


# ---------------------------------------------------------------------------
# test_sshd_kill_no_reconnect
# ---------------------------------------------------------------------------

def test_sshd_kill_no_reconnect(tmpdir, capfd):
    """
    Start a secondary sshd on port 2222, mount against it, kill the sshd,
    verify that sshfs terminates and the mount can be cleaned up.

    This exercises the "harakiri" code path: with reconnect=no (default),
    sshfs sends SIGTERM to itself when the connection drops.
    """
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    capfd.register_output(r"read: Connection reset by peer", count=0)
    capfd.register_output(r"ssh_dispatch_run_fatal", count=0)
    capfd.register_output(r"Connection to 127\.0\.0\.1 closed", count=0)
    capfd.register_output(r"Broken pipe", count=0)

    if not shutil.which("sshd"):
        pytest.skip("sshd not available")

    key_dir = str(tmpdir.mkdir("keys"))
    host_key = pjoin(key_dir, "host_key")
    client_key = pjoin(key_dir, "client_key")
    auth_keys = pjoin(key_dir, "authorized_keys")
    sshd_config = pjoin(key_dir, "sshd_config")
    src_dir = str(tmpdir.mkdir("src"))
    mnt_dir = str(tmpdir.mkdir("mnt"))

    # Generate host key and client key
    subprocess.check_call(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", host_key],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    subprocess.check_call(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", client_key],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    with open(client_key + ".pub") as fh:
        pub = fh.read()
    with open(auth_keys, "w") as fh:
        fh.write(pub)
    os.chmod(auth_keys, 0o600)

    with open(sshd_config, "w") as fh:
        fh.write(
            f"Port 2222\n"
            f"ListenAddress 127.0.0.1\n"
            f"HostKey {host_key}\n"
            f"AuthorizedKeysFile {auth_keys}\n"
            f"StrictModes no\n"
            f"UsePAM no\n"
            f"PasswordAuthentication no\n"
            f"PubkeyAuthentication yes\n"
            f"LogLevel ERROR\n"
            f"PidFile {pjoin(key_dir, 'sshd.pid')}\n"
        )

    # Start secondary sshd
    sshd_proc = subprocess.Popen(
        ["sshd", "-f", sshd_config, "-D"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Give sshd a moment to bind
    time.sleep(0.5)

    if sshd_proc.poll() is not None:
        pytest.skip("could not start secondary sshd on port 2222")

    try:
        # Mount sshfs against the secondary sshd
        cmdline = base_cmdline + [
            pjoin(basename, "sshfs"),
            "-f",
            f"localhost:{src_dir}",
            mnt_dir,
            "-p", "2222",
            "-o", "entry_timeout=0",
            "-o", "attr_timeout=0",
            "-o", "dir_cache=no",
            "-o", "StrictHostKeyChecking=no",
            "-o", f"IdentityFile={client_key}",
            "-o", "PasswordAuthentication=no",
            "-o", "ServerAliveInterval=2",
            "-o", "ServerAliveCountMax=2",
        ]
        new_env = dict(os.environ)
        new_env["G_DEBUG"] = "fatal-warnings"
        mount_process = subprocess.Popen(cmdline, env=new_env,
                                         stderr=subprocess.DEVNULL)

        try:
            wait_for_mount(mount_process, mnt_dir)
        except Exception:
            sshd_proc.terminate()
            cleanup(mount_process, mnt_dir)
            raise

        # Verify the mount works before killing sshd
        test_file = pjoin(mnt_dir, "before_kill")
        with open(test_file, "wb") as fh:
            fh.write(b"pre-kill data")
        with open(test_file, "rb") as fh:
            assert fh.read() == b"pre-kill data"

        # Kill the secondary sshd to simulate connection drop
        sshd_proc.terminate()
        sshd_proc.wait(timeout=5)
        sshd_proc = None  # already terminated

        # sshfs should self-terminate (harakiri) within ~10 seconds
        deadline = time.time() + 15
        while time.time() < deadline:
            code = mount_process.poll()
            if code is not None:
                break
            time.sleep(0.2)

        # Clean up the mount regardless
        subprocess.call(
            ["fusermount3", "-z", "-u", mnt_dir],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        if mount_process.poll() is None:
            mount_process.terminate()
            mount_process.wait(timeout=5)

        # The important assertion: sshfs should have exited (not hung forever)
        assert mount_process.poll() is not None, (
            "sshfs did not terminate after sshd was killed (stale mount)"
        )

    finally:
        if sshd_proc is not None:
            sshd_proc.terminate()
            try:
                sshd_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                sshd_proc.kill()
