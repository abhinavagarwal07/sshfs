#!/usr/bin/env python3

if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main([__file__] + sys.argv[1:]))

import errno
import os
import random
import shutil
import signal
import subprocess
import sys
import time
import pytest
from os.path import join as pjoin
from util import _mount_sshfs, cleanup, fuse_test_marker, mounted, safe_sleep

pytestmark = fuse_test_marker()


def test_permission_errors(tmpdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    if os.getuid() == 0:
        pytest.skip("root is not subject to permission checks")

    with mounted(tmpdir) as (mnt_dir, src_dir):
        locked_dir = pjoin(src_dir, "locked_dir")
        os.mkdir(locked_dir)
        with open(pjoin(locked_dir, "victim"), "wb") as fh:
            fh.write(b"victim")
        secret = pjoin(src_dir, "secret")
        with open(secret, "wb") as fh:
            fh.write(b"secret data")
        readonly = pjoin(src_dir, "readonly")
        with open(readonly, "wb") as fh:
            fh.write(b"readonly data")

        os.chmod(secret, 0o000)
        os.chmod(readonly, 0o444)
        os.chmod(locked_dir, 0o500)
        try:
            # unreadable file
            with pytest.raises(OSError) as exc_info:
                open(pjoin(mnt_dir, "secret"), "rb")
            assert exc_info.value.errno == errno.EACCES

            # read-only file opened for writing
            with pytest.raises(OSError) as exc_info:
                open(pjoin(mnt_dir, "readonly"), "wb")
            assert exc_info.value.errno == errno.EACCES

            # directory that can't be written to
            with pytest.raises(OSError) as exc_info:
                open(pjoin(mnt_dir, "locked_dir", "should_fail"), "wb")
            assert exc_info.value.errno == errno.EACCES
            with pytest.raises(OSError) as exc_info:
                os.unlink(pjoin(mnt_dir, "locked_dir", "victim"))
            assert exc_info.value.errno == errno.EACCES

            # and none of that changed anything
            assert os.listdir(pjoin(mnt_dir, "locked_dir")) == ["victim"]
            with open(pjoin(mnt_dir, "readonly"), "rb") as fh:
                assert fh.read() == b"readonly data"
        finally:
            # so that the temporary directory can be removed
            os.chmod(locked_dir, 0o700)
            os.chmod(secret, 0o600)


def privileged(cmd):
    """Prefix *cmd* with sudo unless we are root already"""
    if os.getuid() == 0:
        return cmd
    if not shutil.which("sudo") or subprocess.call(["sudo", "-n", "true"]) != 0:
        pytest.skip("needs root or passwordless sudo to mount a tmpfs")
    return ["sudo", "-n"] + cmd


def test_disk_full_enospc(tmpdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)

    # A 1 MB tmpfs, owned by us, as the remote directory
    full_dir = str(tmpdir.mkdir("tmpfs"))
    opts = f"size=1M,uid={os.getuid()},gid={os.getgid()},mode=0755"
    subprocess.check_call(
        privileged(["mount", "-t", "tmpfs", "-o", opts, "tmpfs", full_dir]),
        timeout=30,
    )
    try:
        with mounted(tmpdir, ["dir_cache=no"], full_dir) as (mnt_dir, _):
            sentinel = pjoin(mnt_dir, "sentinel")
            with open(sentinel, "wb") as fh:
                fh.write(b"written before the disk was full\n")
                fh.flush()
                os.fsync(fh.fileno())

            # Fill the file system, one 4 KB file at a time
            created = []
            error = None
            for i in range(1000):
                name = pjoin(mnt_dir, f"fill_{i}.bin")
                created.append(name)
                try:
                    with open(name, "wb") as fh:
                        fh.write(b"\0" * 4096)
                        fh.flush()
                        os.fsync(fh.fileno())
                except OSError as exc:
                    error = exc
                    break
            assert error is not None, "the file system never filled up"
            # sshfs turns a failed SFTP write into EIO
            assert error.errno in (errno.ENOSPC, errno.EIO), error

            # sshfs is still there and the data from before is fine
            with open(sentinel, "rb") as fh:
                assert fh.read() == b"written before the disk was full\n"

            # and it works again once there is space
            for name in created:
                try:
                    os.unlink(name)
                except FileNotFoundError:
                    pass
            with open(pjoin(mnt_dir, "after"), "wb") as fh:
                fh.write(b"there is space again\n")
                fh.flush()
                os.fsync(fh.fileno())
            with open(pjoin(full_dir, "after"), "rb") as fh:
                assert fh.read() == b"there is space again\n"
    finally:
        # lazy, in case the ssh processes are still on their way out
        subprocess.call(privileged(["umount", "-l", full_dir]), timeout=30)


# The tests below break the connection by killing the ssh process that sshfs
# started. To find it, ssh is started through a wrapper that records its pid.


def write_ssh_wrapper(tmpdir):
    pidfile = str(tmpdir.join("ssh.pid"))
    wrapper = str(tmpdir.join("ssh-wrapper"))
    with open(wrapper, "w") as fh:
        fh.write(f"#!/bin/sh\necho $$ > '{pidfile}'\nexec ssh \"$@\"\n")
    os.chmod(wrapper, 0o755)
    return wrapper, pidfile


def read_pid(pidfile, previous=None, timeout=15):
    """Wait until pidfile holds a pid other than *previous*"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with open(pidfile) as fh:
                pid = int(fh.read().strip())
        except (FileNotFoundError, ValueError):
            pid = None
        if pid is not None and pid != previous:
            return pid
        safe_sleep(0.05)
    pytest.fail(f"no new ssh pid in {pidfile}")


def test_disconnect_exits(tmpdir, capfd):
    """Without -o reconnect, sshfs exits when the connection goes away"""
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    capfd.register_output(r"^remote host has disconnected$", count=0)

    wrapper, pidfile = write_ssh_wrapper(tmpdir)
    mount_process, mnt_dir, src_dir = _mount_sshfs(tmpdir, [f"ssh_command={wrapper}"])
    try:
        ssh_pid = read_pid(pidfile)
        with open(pjoin(mnt_dir, "before"), "wb") as fh:
            fh.write(b"written while connected")

        os.kill(ssh_pid, signal.SIGKILL)

        try:
            code = mount_process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pytest.fail("sshfs is still running 15s after its ssh process died")
        assert code >= 0, f"sshfs was killed by signal {-code}"
        assert not os.path.ismount(mnt_dir)
    finally:
        if mount_process.poll() is None:
            cleanup(mount_process, mnt_dir)

    with open(pjoin(src_dir, "before"), "rb") as fh:
        assert fh.read() == b"written while connected"


def test_reconnect(tmpdir, capfd):
    """With -o reconnect, sshfs starts a new connection when the old one dies"""
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    capfd.register_output(r"^remote host has disconnected$", count=0)

    wrapper, pidfile = write_ssh_wrapper(tmpdir)
    with mounted(tmpdir, [f"ssh_command={wrapper}", "reconnect"]) as (mnt_dir, src_dir):
        old_pid = read_pid(pidfile)
        with open(pjoin(mnt_dir, "before"), "wb") as fh:
            fh.write(b"written on the first connection")

        os.kill(old_pid, signal.SIGKILL)

        # The first requests after the disconnect may fail while sshfs
        # notices and reconnects, so retry for a while.
        deadline = time.monotonic() + 30
        while True:
            try:
                with open(pjoin(mnt_dir, "after"), "wb") as fh:
                    fh.write(b"written on the second connection")
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                safe_sleep(0.2)

        assert read_pid(pidfile, previous=old_pid) != old_pid
        with open(pjoin(mnt_dir, "before"), "rb") as fh:
            assert fh.read() == b"written on the first connection"
        with open(pjoin(src_dir, "after"), "rb") as fh:
            assert fh.read() == b"written on the second connection"


# Forwards between its stdin/stdout and sshd on localhost, holding back every
# chunk of data for a while. Used as ssh's ProxyCommand.
DELAY_PROXY = """
import os, socket, sys, threading, time

DELAY = 0.02
sock = socket.create_connection(("localhost", int(sys.argv[1])))


def write_all(fd, data):
    while data:
        data = data[os.write(fd, data):]


def pump(read, write):
    while True:
        data = read(65536)
        if not data:
            return
        time.sleep(DELAY)
        write(data)


def client_to_server():
    try:
        pump(lambda n: os.read(0, n), sock.sendall)
        sock.shutdown(socket.SHUT_WR)
    except OSError:
        pass


threading.Thread(target=client_to_server, daemon=True).start()
try:
    pump(sock.recv, lambda data: write_all(1, data))
except OSError:
    pass
"""


def ssh_port():
    out = subprocess.check_output(["ssh", "-G", "localhost"], text=True, timeout=10)
    for line in out.splitlines():
        if line.startswith("port "):
            return int(line.split()[1])
    pytest.skip("could not work out the ssh port")


def test_latency(tmpdir, capfd):
    """Basic operations over a connection that takes 20 ms per hop"""
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)

    proxy = tmpdir.join("delay_proxy.py")
    proxy.write(DELAY_PROXY)
    proxy_cmd = f"{sys.executable} {proxy} {ssh_port()}"

    with mounted(tmpdir, ["dir_cache=no", f"ProxyCommand={proxy_cmd}"]) as (
        mnt_dir,
        src_dir,
    ):
        path = pjoin(mnt_dir, "latency_test")
        with open(path, "wb") as fh:
            fh.write(b"hello under latency\n")

        # Every reply has to pass through the proxy, so this can't be faster
        # than one delay. Makes sure the proxy is in the path at all.
        start = time.monotonic()
        st = os.stat(path)
        assert time.monotonic() - start >= 0.02
        assert st.st_size == 20

        with open(path, "rb") as fh:
            assert fh.read() == b"hello under latency\n"

        with open(path, "ab") as fh:
            fh.write(b"appended\n")
        with open(path, "rb") as fh:
            assert fh.read() == b"hello under latency\nappended\n"

        renamed = pjoin(mnt_dir, "latency_renamed")
        os.rename(path, renamed)
        assert os.listdir(mnt_dir) == ["latency_renamed"]

        # something that needs more than one request
        big = random.Random(7).randbytes(256 * 1024)
        with open(pjoin(mnt_dir, "big"), "wb") as fh:
            fh.write(big)
        with open(pjoin(mnt_dir, "big"), "rb") as fh:
            assert fh.read() == big
        with open(pjoin(src_dir, "big"), "rb") as fh:
            assert fh.read() == big

        os.unlink(renamed)
        os.unlink(pjoin(mnt_dir, "big"))
        assert os.listdir(mnt_dir) == []
