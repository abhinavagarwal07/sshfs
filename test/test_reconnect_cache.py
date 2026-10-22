#!/usr/bin/env python3

if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main([__file__] + sys.argv[1:]))

import os
import shlex
import signal
import time
from contextlib import contextmanager
from os.path import join as pjoin

import pytest

from test_sshfs import _mount_sshfs
from util import cleanup, fuse_test_marker, safe_sleep, umount


pytestmark = fuse_test_marker()


def _register_expected_disconnect_output(capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    capfd.register_output(r"^remote host has disconnected$", count=0)
    capfd.register_output(r"^read: .+$", count=0)
    capfd.register_output(r"^write: .+$", count=0)


def _write_ssh_wrapper(tmpdir):
    """An ssh_command that records the pid of the ssh it runs"""
    pidfile = str(tmpdir.join("ssh.pid"))
    wrapper = str(tmpdir.join("ssh-wrapper"))
    with open(wrapper, "w", encoding="utf-8") as fh:
        fh.write(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$$\" > {shlex.quote(pidfile)}\n"
            'exec ssh "$@"\n'
        )
    os.chmod(wrapper, 0o755)
    return wrapper, pidfile


def _read_pid(pidfile, previous=None, timeout=10.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            with open(pidfile, "r", encoding="utf-8") as fh:
                last = int(fh.read().strip())
        except (FileNotFoundError, ValueError):
            pass
        else:
            if last != previous:
                return last
        safe_sleep(0.05)
    pytest.fail(f"timed out waiting for ssh pid in {pidfile}; last={last!r}")


def _eventually(call, predicate, description, timeout=10.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            value = call()
        except OSError as exc:
            last = exc
        else:
            if predicate(value):
                return value
            last = value
        safe_sleep(0.1)
    pytest.fail(f"{description}: timed out; last={last!r}")


@contextmanager
def _mounted_with_recorded_ssh(tmpdir, extra_opts):
    wrapper, pidfile = _write_ssh_wrapper(tmpdir)
    mount_process, mnt_dir, src_dir = _mount_sshfs(
        tmpdir, [f"ssh_command={wrapper}"] + extra_opts
    )
    try:
        _read_pid(pidfile)
        yield mount_process, mnt_dir, src_dir, pidfile
    except BaseException:
        # pytest.fail() and pytest.skip() are not Exceptions
        cleanup(mount_process, mnt_dir)
        raise
    else:
        umount(mount_process, mnt_dir)


def _reconnect(pidfile, probe):
    """Drop the ssh session and wait until sshfs is talking to a new one

    sshfs only reconnects when a request needs the connection, so look up a
    path that cannot be cached.
    """
    old = _read_pid(pidfile)
    os.kill(old, signal.SIGKILL)
    _eventually(lambda: os.stat(probe), lambda _: True, "lookup after the drop")
    _read_pid(pidfile, previous=old)


def test_reconnect_refetches_cached_directory(tmpdir, capfd):
    _register_expected_disconnect_output(capfd)

    # A cache lifetime longer than the test, so only a purge can refresh it
    with _mounted_with_recorded_ssh(
        tmpdir, ["reconnect", "dcache_timeout=600"]
    ) as (_mount_process, mnt_dir, src_dir, pidfile):
        src_subdir = pjoin(src_dir, "dir")
        mnt_subdir = pjoin(mnt_dir, "dir")
        os.mkdir(src_subdir)
        with open(pjoin(src_subdir, "old"), "wb") as fh:
            fh.write(b"old")
        with open(pjoin(src_dir, "probe"), "wb") as fh:
            fh.write(b"probe")

        assert sorted(os.listdir(mnt_subdir)) == ["old"]

        os.unlink(pjoin(src_subdir, "old"))
        with open(pjoin(src_subdir, "new"), "wb") as fh:
            fh.write(b"new")

        _reconnect(pidfile, pjoin(mnt_dir, "probe"))

        assert sorted(os.listdir(mnt_subdir)) == ["new"]


def test_reconnect_refetches_cached_symlink(tmpdir, capfd):
    _register_expected_disconnect_output(capfd)

    with _mounted_with_recorded_ssh(
        tmpdir, ["reconnect", "dcache_timeout=600"]
    ) as (_mount_process, mnt_dir, src_dir, pidfile):
        os.symlink("target-before", pjoin(src_dir, "link"))
        with open(pjoin(src_dir, "probe"), "wb") as fh:
            fh.write(b"probe")

        assert os.readlink(pjoin(mnt_dir, "link")) == "target-before"

        os.unlink(pjoin(src_dir, "link"))
        os.symlink("target-after", pjoin(src_dir, "link"))

        _reconnect(pidfile, pjoin(mnt_dir, "probe"))

        assert os.readlink(pjoin(mnt_dir, "link")) == "target-after"
