#!/usr/bin/env python3

if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main([__file__] + sys.argv[1:]))

import errno
import os
import signal
import subprocess
import time
from os.path import join as pjoin

import pytest

from util import base_cmdline, basename, cleanup, safe_sleep


def _write_blocking_ssh_command(tmpdir):
    """An ssh_command that records its pid, signals that it runs, and then
    ignores its stdin forever, like an ssh stuck in connect()"""
    readyfile = str(tmpdir.join("ssh.ready"))
    pidfile = str(tmpdir.join("ssh-blocked.pid"))
    helper = str(tmpdir.join("ssh-blocked"))
    with open(helper, "w", encoding="utf-8") as fh:
        fh.write(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$$\" > '{pidfile}'\n"
            f": > '{readyfile}'\n"
            "trap 'exit 0' TERM\n"
            "while :; do sleep 1; done\n"
        )
    os.chmod(helper, 0o755)
    return helper, readyfile, pidfile


def _eventually(call, predicate, description, timeout=10.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        value = call()
        if predicate(value):
            return value
        last = value
        safe_sleep(0.1)
    pytest.fail(f"{description}: timed out; last={last!r}")


def _read_pid(pidfile, timeout=5.0):
    def read():
        try:
            with open(pidfile, "r", encoding="utf-8") as fh:
                return int(fh.read().strip())
        except (FileNotFoundError, ValueError):
            return None

    return _eventually(read, lambda pid: pid is not None,
                       f"pid in {pidfile}", timeout=timeout)


def _pid_exists(pid):
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        raise
    return True


def test_sigint_during_blocked_startup_kills_helper(tmpdir, capfd):
    capfd.register_output(r"^read: Interrupted system call$", count=0)
    helper, readyfile, pidfile = _write_blocking_ssh_command(tmpdir)
    mnt_dir = str(tmpdir.mkdir("mnt-startup"))
    cmdline = base_cmdline + [
        pjoin(basename, "sshfs"),
        "-f",
        "-o", f"ssh_command={helper}",
        "localhost:/",
        mnt_dir,
    ]

    mount_process = subprocess.Popen(cmdline)
    helper_pid = None
    try:
        _eventually(lambda: os.path.exists(readyfile), lambda ok: ok,
                    f"{readyfile} to exist")
        helper_pid = _read_pid(pidfile)
        os.kill(mount_process.pid, signal.SIGINT)
        try:
            rc = mount_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pytest.fail("sshfs did not exit after SIGINT")

        assert rc != 0
        assert not os.path.ismount(mnt_dir)
        _eventually(
            lambda: _pid_exists(helper_pid),
            lambda exists: not exists,
            "ssh helper to exit after sshfs was interrupted",
        )
    finally:
        if mount_process.poll() is None:
            cleanup(mount_process, mnt_dir)
        if helper_pid is not None and _pid_exists(helper_pid):
            os.kill(helper_pid, signal.SIGTERM)
