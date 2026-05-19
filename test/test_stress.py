#!/usr/bin/env python3

if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main([__file__] + sys.argv[1:]))

import errno
import fcntl
import multiprocessing
import os
import shutil
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
# test_fsx
# ---------------------------------------------------------------------------

def test_fsx(tmpdir, capfd):
    """
    Run fsx-linux (from ltp) with 10K random ops against the sshfs mount.
    Fixed seed makes failures reproducible.
    """
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)

    # fsx-linux may be at different paths depending on distro
    fsx_candidates = [
        "/usr/lib/ltp/testcases/bin/fsx-linux",
        "/usr/lib/ltp/testcases/bin/fsx",
        shutil.which("fsx-linux"),
        shutil.which("fsx"),
    ]
    fsx_bin = next((p for p in fsx_candidates if p and os.path.isfile(p)), None)
    if fsx_bin is None:
        pytest.skip("fsx-linux not found (install ltp package)")

    mount_process, mnt_dir, src_dir = _mount_sshfs(tmpdir, ["dir_cache=no"])
    try:
        fsx_file = pjoin(mnt_dir, "fsx-testfile")
        result = subprocess.run(
            [fsx_bin, "-N", "10000", "-l", str(1024 * 1024), "-S", "42", fsx_file],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            pytest.fail(
                f"fsx-linux failed (exit {result.returncode}):\n"
                f"stdout: {result.stdout[-3000:]}\n"
                f"stderr: {result.stderr[-1000:]}"
            )
    except Exception:
        cleanup(mount_process, mnt_dir)
        raise
    else:
        umount(mount_process, mnt_dir)


# ---------------------------------------------------------------------------
# test_fsstress
# ---------------------------------------------------------------------------

def test_fsstress(tmpdir, capfd):
    """
    Run fsstress (from xfstests) with concurrent random directory operations.
    Compiles fsstress from xfstests source if not already available.
    """
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)

    fsstress_bin = shutil.which("fsstress")
    if fsstress_bin is None:
        # Try to find it in a known build location
        fsstress_bin = "/tmp/xfstests/ltp/fsstress"
        if not os.path.isfile(fsstress_bin):
            pytest.skip(
                "fsstress not found; build xfstests first: "
                "git clone --depth=1 https://github.com/kdave/xfstests /tmp/xfstests && "
                "cd /tmp/xfstests && make ltp"
            )

    mount_process, mnt_dir, src_dir = _mount_sshfs(tmpdir, ["dir_cache=no"])
    try:
        stress_dir = pjoin(mnt_dir, "stress")
        os.makedirs(stress_dir)

        result = subprocess.run(
            [fsstress_bin, "-d", stress_dir, "-n", "500", "-p", "4", "-r"],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode != 0:
            pytest.fail(
                f"fsstress failed (exit {result.returncode}):\n"
                f"stdout: {result.stdout[-3000:]}\n"
                f"stderr: {result.stderr[-1000:]}"
            )
    except Exception:
        cleanup(mount_process, mnt_dir)
        raise
    else:
        umount(mount_process, mnt_dir)


# ---------------------------------------------------------------------------
# test_concurrent_readers_during_write
# ---------------------------------------------------------------------------

def _reader_worker(path, stop_event, results):
    """Continuously read a file and verify it is never a mix of old/new data."""
    marker_a = b"AAAA" * (1024 * 1024 // 4)
    marker_b = b"BBBB" * (1024 * 1024 // 4)
    size = len(marker_a)

    errors = []
    while not stop_event.is_set():
        try:
            with open(path, "rb") as fh:
                data = fh.read()
            if len(data) != size:
                continue  # partial write in progress, acceptable
            # Data must be entirely A's or entirely B's
            if data not in (marker_a, marker_b):
                errors.append(f"torn read: got mixed content of length {len(data)}")
        except OSError:
            pass  # file may not exist yet

    results.extend(errors)


def test_concurrent_readers_during_write(tmpdir, capfd):
    """One writer overwrites a 4MB file repeatedly; 4 readers verify no torn reads."""
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)

    mount_process, mnt_dir, src_dir = _mount_sshfs(
        tmpdir, ["dir_cache=no", "max_conns=4"]
    )
    try:
        path = pjoin(mnt_dir, "shared_file")
        SIZE = 4 * 1024 * 1024
        data_a = b"A" * SIZE
        data_b = b"B" * SIZE

        # Write initial content
        with open(path, "wb") as fh:
            fh.write(data_a)

        stop_event = threading.Event()
        reader_results = []
        readers = []

        for _ in range(4):
            t = threading.Thread(
                target=_reader_worker,
                args=(path, stop_event, reader_results),
                daemon=True,
            )
            t.start()
            readers.append(t)

        # Writer: alternate between A and B for 5 seconds
        deadline = time.time() + 5
        toggle = True
        while time.time() < deadline:
            data = data_a if toggle else data_b
            with open(path, "wb") as fh:
                fh.write(data)
            toggle = not toggle

        stop_event.set()
        for t in readers:
            t.join(timeout=5)

        assert not reader_results, (
            f"readers detected torn writes:\n" + "\n".join(reader_results[:10])
        )
    except Exception:
        cleanup(mount_process, mnt_dir)
        raise
    else:
        umount(mount_process, mnt_dir)


# ---------------------------------------------------------------------------
# test_dual_mount
# ---------------------------------------------------------------------------

def test_dual_mount(tmpdir, capfd):
    """
    Mount the same src_dir twice. Verify a file created via one mount
    is visible from the other (within cache timeout).
    """
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)

    _check_ssh_localhost()

    src_dir = str(tmpdir.mkdir("shared_src"))
    mnt1 = str(tmpdir.mkdir("mnt1"))
    mnt2 = str(tmpdir.mkdir("mnt2"))

    def _start_mount(mnt_dir):
        cmdline = base_cmdline + [
            pjoin(basename, "sshfs"),
            "-f",
            f"localhost:{src_dir}",
            mnt_dir,
            "-o", "entry_timeout=0",
            "-o", "attr_timeout=0",
            "-o", "dir_cache=no",
        ]
        new_env = dict(os.environ)
        new_env["G_DEBUG"] = "fatal-warnings"
        proc = subprocess.Popen(cmdline, env=new_env)
        wait_for_mount(proc, mnt_dir)
        return proc

    proc1 = _start_mount(mnt1)
    proc2 = _start_mount(mnt2)

    try:
        # Create a file via mnt1, verify it appears in mnt2
        path1 = pjoin(mnt1, "shared_file")
        with open(path1, "wb") as fh:
            fh.write(b"written via mnt1")

        # With dir_cache=no, the file should be visible immediately
        path2 = pjoin(mnt2, "shared_file")
        assert os.path.exists(path2), "file created via mnt1 not visible in mnt2"
        with open(path2, "rb") as fh:
            assert fh.read() == b"written via mnt1"

        # Write via mnt2, verify content via mnt1
        with open(path2, "wb") as fh:
            fh.write(b"overwritten via mnt2")
        with open(path1, "rb") as fh:
            assert fh.read() == b"overwritten via mnt2"

    except Exception:
        cleanup(proc1, mnt1)
        cleanup(proc2, mnt2)
        raise
    else:
        umount(proc1, mnt1)
        umount(proc2, mnt2)


# ---------------------------------------------------------------------------
# test_sshd_kill_reconnect
# ---------------------------------------------------------------------------

def test_sshd_kill_reconnect(tmpdir, capfd):
    """
    Kill sshd mid-mount with reconnect=yes. After restarting sshd,
    verify that file operations resume successfully.
    """
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    capfd.register_output(r"read: Connection reset by peer", count=0)
    capfd.register_output(r"ssh_dispatch_run_fatal", count=0)
    capfd.register_output(r"Connection to 127\.0\.0\.1 closed", count=0)
    capfd.register_output(r"Broken pipe", count=0)
    capfd.register_output(r"reconnect", count=0)

    if not shutil.which("sshd"):
        pytest.skip("sshd not available")

    key_dir = str(tmpdir.mkdir("keys"))
    host_key = pjoin(key_dir, "host_key")
    client_key = pjoin(key_dir, "client_key")
    auth_keys = pjoin(key_dir, "authorized_keys")
    sshd_config = pjoin(key_dir, "sshd_config")
    src_dir = str(tmpdir.mkdir("src"))
    mnt_dir = str(tmpdir.mkdir("mnt"))

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
            f"Port 2223\n"
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

    def _start_sshd():
        return subprocess.Popen(
            ["sshd", "-f", sshd_config, "-D"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    sshd_proc = _start_sshd()
    time.sleep(0.5)
    if sshd_proc.poll() is not None:
        pytest.skip("could not start secondary sshd on port 2223")

    cmdline = base_cmdline + [
        pjoin(basename, "sshfs"),
        "-f",
        f"localhost:{src_dir}",
        mnt_dir,
        "-p", "2223",
        "-o", "entry_timeout=0",
        "-o", "attr_timeout=0",
        "-o", "dir_cache=no",
        "-o", "reconnect",
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

        # Write a file before kill
        pre_kill = pjoin(mnt_dir, "pre_kill")
        with open(pre_kill, "wb") as fh:
            fh.write(b"before kill")

        # Kill sshd
        sshd_proc.terminate()
        sshd_proc.wait(timeout=5)

        # Restart sshd
        time.sleep(1)
        sshd_proc = _start_sshd()
        time.sleep(1)

        # After reconnect, operations should succeed again
        # (give sshfs up to 15 seconds to reconnect)
        post_kill = pjoin(mnt_dir, "post_kill")
        deadline = time.time() + 15
        last_exc = None
        while time.time() < deadline:
            try:
                with open(post_kill, "wb") as fh:
                    fh.write(b"after reconnect")
                last_exc = None
                break
            except OSError as e:
                last_exc = e
                time.sleep(0.5)

        if last_exc is not None:
            pytest.fail(f"sshfs did not reconnect within 15s: {last_exc}")

        with open(post_kill, "rb") as fh:
            assert fh.read() == b"after reconnect"

    except Exception:
        if sshd_proc and sshd_proc.poll() is None:
            sshd_proc.terminate()
        subprocess.call(
            ["fusermount3", "-z", "-u", mnt_dir],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if mount_process.poll() is None:
            mount_process.terminate()
            try:
                mount_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                mount_process.kill()
        raise
    else:
        if sshd_proc and sshd_proc.poll() is None:
            sshd_proc.terminate()
            sshd_proc.wait(timeout=3)
        umount(mount_process, mnt_dir)


# ---------------------------------------------------------------------------
# test_flock
# ---------------------------------------------------------------------------

def _flock_holder(path, lock_held, unlock_event):
    """Acquire an exclusive flock, signal lock_held, wait for unlock_event."""
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        lock_held.set()
        unlock_event.wait(timeout=10)
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def test_flock(tmpdir, capfd):
    """
    Verify flock(LOCK_EX) from one thread blocks another thread on the same
    sshfs mount. FUSE-level locking is local to the client machine.
    """
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)

    mount_process, mnt_dir, src_dir = _mount_sshfs(tmpdir, ["dir_cache=no"])
    try:
        lock_file = pjoin(mnt_dir, "lockfile")
        with open(lock_file, "wb") as fh:
            fh.write(b"lock target")

        lock_held = threading.Event()
        unlock_event = threading.Event()

        holder = threading.Thread(
            target=_flock_holder,
            args=(lock_file, lock_held, unlock_event),
            daemon=True,
        )
        holder.start()
        lock_held.wait(timeout=5)
        assert lock_held.is_set(), "holder thread did not acquire lock"

        # Try to acquire the lock with LOCK_NB — should fail with EWOULDBLOCK
        fd2 = os.open(lock_file, os.O_RDWR)
        try:
            with pytest.raises(OSError) as exc_info:
                fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert exc_info.value.errno in (errno.EWOULDBLOCK, errno.EAGAIN), (
                f"expected EWOULDBLOCK, got errno {exc_info.value.errno}"
            )
        finally:
            os.close(fd2)

        # Release the lock
        unlock_event.set()
        holder.join(timeout=5)

        # Now the lock should be acquirable
        fd3 = os.open(lock_file, os.O_RDWR)
        try:
            fcntl.flock(fd3, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd3, fcntl.LOCK_UN)
        finally:
            os.close(fd3)

    except Exception:
        cleanup(mount_process, mnt_dir)
        raise
    else:
        umount(mount_process, mnt_dir)
