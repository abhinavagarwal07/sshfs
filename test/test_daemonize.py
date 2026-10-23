#!/usr/bin/env python3
"""Without -f, sshfs has to detach and keep serving the mount.

Every other test runs sshfs with -f, so fuse_daemonize() is not exercised
anywhere else. On macFUSE 5.3.3+ a stray fuse_session_fd() before the fork made
sshfs stay in the foreground (upstream issue 373).
"""

if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main([__file__] + sys.argv[1:]))

import os
import subprocess
import time
import pytest
from os.path import join as pjoin
from util import base_cmdline, basename, fuse_test_marker

pytestmark = fuse_test_marker()


def daemon_pids(mnt_dir):
    """PIDs of the sshfs processes serving *mnt_dir*, like pgrep -f"""
    pids = []
    for entry in os.listdir("/proc"):
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as fh:
                args = fh.read().split(b"\0")
        except OSError:
            continue
        if mnt_dir.encode() in args and any(a.endswith(b"sshfs") for a in args):
            pids.append(int(entry))
    return sorted(pids)


def wait_until(cond, what, secs=30):
    end = time.monotonic() + secs
    while not cond():
        if time.monotonic() > end:
            pytest.fail(what)
        time.sleep(0.1)


def test_daemonizes_without_f(tmpdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)

    mnt_dir = str(tmpdir.mkdir("mnt"))
    src_dir = str(tmpdir.mkdir("src"))
    with open(pjoin(src_dir, "hello"), "w") as fh:
        fh.write("hello")

    cmdline = base_cmdline + [
        os.path.abspath(pjoin(basename, "sshfs")),
        f"localhost:{src_dir}",
        mnt_dir,
        "-o", "StrictHostKeyChecking=no",
        "-o", "BatchMode=yes",
    ]
    env = dict(os.environ, G_DEBUG="fatal-warnings")
    try:
        try:
            res = subprocess.run(
                cmdline, env=env, stdin=subprocess.DEVNULL, timeout=30
            )
        except subprocess.TimeoutExpired:
            pytest.fail("sshfs did not return, it stayed in the foreground")
        assert res.returncode == 0
        pids = daemon_pids(mnt_dir)
        assert pids, "no daemon is serving the mount"

        with open(pjoin(mnt_dir, "hello")) as fh:
            assert fh.read() == "hello"
        with open(pjoin(mnt_dir, "new"), "w") as fh:
            fh.write("world")
        with open(pjoin(src_dir, "new")) as fh:
            assert fh.read() == "world"

        assert daemon_pids(mnt_dir) == pids, "the daemon died while serving"
        subprocess.check_call(["fusermount3", "-u", mnt_dir], timeout=30)
        wait_until(
            lambda: not daemon_pids(mnt_dir), "daemon still running after unmount"
        )
    finally:
        try:
            subprocess.call(
                ["fusermount3", "-u", "-z", mnt_dir],
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
        finally:
            for pid in daemon_pids(mnt_dir):
                try:
                    os.kill(pid, 9)
                except ProcessLookupError:
                    pass
