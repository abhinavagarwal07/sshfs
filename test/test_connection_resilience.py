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
from contextlib import contextmanager
from os.path import join as pjoin

import pytest

from test_sshfs import _mount_sshfs
from util import base_cmdline, basename, cleanup, fuse_test_marker, safe_sleep, umount


pytestmark = fuse_test_marker()


def _register_expected_disconnect_output(capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    capfd.register_output(r"^remote host has disconnected$", count=0)
    capfd.register_output(r"^read: .+$", count=0)
    capfd.register_output(r"^write: .+$", count=0)


def _write_ssh_wrapper(tmpdir):
    pidfile = str(tmpdir.join("ssh.pid"))
    wrapper = str(tmpdir.join("ssh-wrapper"))
    with open(wrapper, "w", encoding="utf-8") as fh:
        fh.write(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$$\" > '{pidfile}'\n"
            "exec ssh \"$@\"\n"
        )
    os.chmod(wrapper, 0o755)
    return wrapper, pidfile


def _write_blocking_ssh_command(tmpdir):
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


def _read_pid(pidfile, previous=None, timeout=5.0):
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


def _wait_for_path(path, timeout=5.0):
    _eventually(lambda: os.path.exists(path), lambda exists: exists,
                f"{path} to exist", timeout=timeout)


def _pid_exists(pid):
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        raise
    return True


def _kill_recorded_ssh(pidfile):
    pid = _read_pid(pidfile)
    os.kill(pid, signal.SIGKILL)
    safe_sleep(0.5)
    return pid


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
    except Exception:
        cleanup(mount_process, mnt_dir)
        raise
    else:
        umount(mount_process, mnt_dir)


def _assert_stale_error(exc_info):
    assert exc_info.value.errno in (errno.EIO, errno.ENOTCONN)


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
        _wait_for_path(readyfile)
        helper_pid = _read_pid(pidfile)
        os.kill(mount_process.pid, signal.SIGINT)
        try:
            rc = mount_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pytest.fail("sshfs did not exit after targeted SIGINT")

        assert rc != 0
        assert not os.path.ismount(mnt_dir)
        _eventually(
            lambda: _pid_exists(helper_pid),
            lambda exists: not exists,
            "blocked ssh helper to exit after sshfs SIGINT",
        )
    finally:
        if mount_process.poll() is None:
            cleanup(mount_process, mnt_dir)
        if helper_pid is not None and _pid_exists(helper_pid):
            os.kill(helper_pid, signal.SIGTERM)


def test_reconnect_refetches_cached_directory_and_readlink(tmpdir, capfd):
    _register_expected_disconnect_output(capfd)

    with _mounted_with_recorded_ssh(tmpdir, ["reconnect"]) as (
        _mount_process,
        mnt_dir,
        src_dir,
        pidfile,
    ):
        src_subdir = pjoin(src_dir, "dir")
        mnt_subdir = pjoin(mnt_dir, "dir")
        os.mkdir(src_subdir)
        with open(pjoin(src_subdir, "old"), "wb") as fh:
            fh.write(b"old")
        os.symlink("target-before", pjoin(src_dir, "link"))

        assert sorted(os.listdir(mnt_subdir)) == ["old"]
        assert os.readlink(pjoin(mnt_dir, "link")) == "target-before"

        _kill_recorded_ssh(pidfile)

        os.unlink(pjoin(src_subdir, "old"))
        with open(pjoin(src_subdir, "new"), "wb") as fh:
            fh.write(b"new")
        os.unlink(pjoin(src_dir, "link"))
        os.symlink("target-after", pjoin(src_dir, "link"))

        _eventually(
            lambda: sorted(os.listdir(mnt_subdir)),
            lambda value: value == ["new"],
            "directory cache after reconnect",
        )
        _eventually(
            lambda: os.readlink(pjoin(mnt_dir, "link")),
            lambda value: value == "target-after",
            "readlink cache after reconnect",
        )


@pytest.mark.parametrize(
    "extra_opts",
    [
        pytest.param([], id="async-readdir-cache"),
        pytest.param(["sync_readdir"], id="sync-readdir-cache"),
        pytest.param(["dir_cache=no"], id="dir-cache-off"),
    ],
)
def test_stale_directory_handle_fails_after_reconnect(tmpdir, capfd, extra_opts):
    if os.listdir not in os.supports_fd:
        pytest.skip("os.listdir(fd) is not supported on this platform")

    _register_expected_disconnect_output(capfd)

    with _mounted_with_recorded_ssh(tmpdir, ["reconnect"] + extra_opts) as (
        _mount_process,
        mnt_dir,
        src_dir,
        pidfile,
    ):
        src_subdir = pjoin(src_dir, "dir")
        mnt_subdir = pjoin(mnt_dir, "dir")
        os.mkdir(src_subdir)
        with open(pjoin(src_subdir, "old"), "wb") as fh:
            fh.write(b"old")

        dirfd = os.open(mnt_subdir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            assert sorted(os.listdir(dirfd)) == ["old"]
            try:
                os.lseek(dirfd, 0, os.SEEK_SET)
            except OSError:
                pytest.skip("directory fd cannot be rewound on this platform")

            _kill_recorded_ssh(pidfile)
            _eventually(
                lambda: sorted(os.listdir(mnt_dir)),
                lambda value: "dir" in value,
                "path operation after reconnect",
            )

            with pytest.raises(OSError) as exc_info:
                os.listdir(dirfd)
            _assert_stale_error(exc_info)
        finally:
            os.close(dirfd)


@pytest.mark.parametrize(
    "extra_opts",
    [
        pytest.param(["direct_io"], id="async-write"),
        pytest.param(["direct_io", "sshfs_sync"], id="sync-write"),
    ],
)
def test_stale_file_handle_io_fsync_fails_after_reconnect(tmpdir, capfd, extra_opts):
    _register_expected_disconnect_output(capfd)

    with _mounted_with_recorded_ssh(tmpdir, ["reconnect"] + extra_opts) as (
        _mount_process,
        mnt_dir,
        src_dir,
        pidfile,
    ):
        src_file = pjoin(src_dir, "file")
        mnt_file = pjoin(mnt_dir, "file")
        with open(src_file, "wb") as fh:
            fh.write(b"before")

        fd = os.open(mnt_file, os.O_RDWR)
        try:
            assert os.read(fd, 1) == b"b"
            _kill_recorded_ssh(pidfile)
            _eventually(
                lambda: sorted(os.listdir(mnt_dir)),
                lambda value: "file" in value,
                "path operation after reconnect",
            )

            with pytest.raises(OSError) as read_exc:
                os.read(fd, 1)
            _assert_stale_error(read_exc)

            with pytest.raises(OSError) as fsync_exc:
                os.fsync(fd)
            _assert_stale_error(fsync_exc)

            with pytest.raises(OSError) as write_exc:
                os.write(fd, b"x")
            _assert_stale_error(write_exc)
        finally:
            try:
                os.close(fd)
            except OSError as exc:
                if exc.errno not in (errno.EIO, errno.ENOTCONN):
                    raise
