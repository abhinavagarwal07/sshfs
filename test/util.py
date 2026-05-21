#!/usr/bin/env python3
import subprocess
import pytest
import os
import stat
import time
import functools
from os.path import join as pjoin
from contextlib import contextmanager

basename = pjoin(os.path.dirname(__file__), "..")


def os_create(name):
    os.close(os.open(name, os.O_CREAT | os.O_RDWR))


@contextmanager
def os_open(name, flags):
    fd = os.open(name, flags)
    try:
        yield fd
    finally:
        os.close(fd)


def wait_for_mount(mount_process, mnt_dir, test_fn=os.path.ismount):
    elapsed = 0
    while elapsed < 30:
        if test_fn(mnt_dir):
            return True
        if mount_process.poll() is not None:
            pytest.fail("file system process terminated prematurely")
        time.sleep(0.1)
        elapsed += 0.1
    pytest.fail("mountpoint failed to come up")


def cleanup(mount_process, mnt_dir):
    subprocess.call(
        ["fusermount", "-z", "-u", mnt_dir],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    mount_process.terminate()
    try:
        mount_process.wait(1)
    except subprocess.TimeoutExpired:
        mount_process.kill()


def umount(mount_process, mnt_dir):
    subprocess.check_call(["fusermount3", "-z", "-u", mnt_dir])
    assert not os.path.ismount(mnt_dir)

    # Give mount process a little while to terminate. Popen.wait(timeout)
    # was only added in 3.3...
    elapsed = 0
    while elapsed < 30:
        code = mount_process.poll()
        if code is not None:
            if code == 0:
                return
            pytest.fail(f"file system process terminated with code {code}")
        time.sleep(0.1)
        elapsed += 0.1
    pytest.fail("mount process did not terminate")


def safe_sleep(secs):
    """Like time.sleep(), but sleep for at least *secs*

    `time.sleep` may sleep less than the given period if a signal is
    received. This function ensures that we sleep for at least the
    desired time.
    """

    now = time.time()
    end = now + secs
    while now < end:
        time.sleep(end - now)
        now = time.time()


def fuse_test_marker():
    """Return a pytest.marker that indicates FUSE availability

    If system/user/environment does not support FUSE, return
    a `pytest.mark.skip` object with more details. If FUSE is
    supported, return `pytest.mark.uses_fuse()`.
    """

    def skip(reason: str):
        return pytest.mark.skip(reason=reason)

    with subprocess.Popen(
        ["which", "fusermount"], stdout=subprocess.PIPE, universal_newlines=True
    ) as which:
        fusermount_path = which.communicate()[0].strip()

    if not fusermount_path or which.returncode != 0:
        return skip("Can't find fusermount executable")

    if not os.path.exists("/dev/fuse"):
        return skip("FUSE kernel module does not seem to be loaded")

    if os.getuid() == 0:
        return pytest.mark.uses_fuse()

    mode = os.stat(fusermount_path).st_mode
    if mode & stat.S_ISUID == 0:
        return skip("fusermount executable not setuid, and we are not root.")

    try:
        fd = os.open("/dev/fuse", os.O_RDWR)
    except OSError as exc:
        return skip(f"Unable to open /dev/fuse: {exc.strerror}")
    else:
        os.close(fd)

    return pytest.mark.uses_fuse()


# Use valgrind if requested
if os.environ.get("TEST_WITH_VALGRIND", "no").lower().strip() not in (
    "no",
    "false",
    "0",
):
    base_cmdline = ["valgrind", "-q", "--"]
else:
    base_cmdline = []


_ssh_checked = False
_ssh_available = False


def _check_ssh_localhost():
    global _ssh_checked, _ssh_available
    if _ssh_checked:
        if not _ssh_available:
            pytest.skip("Unable to ssh into localhost without password prompt.")
        return
    _ssh_checked = True
    try:
        res = subprocess.call(
            ["ssh", "-o", "StrictHostKeyChecking=no",
             "-o", "KbdInteractiveAuthentication=no",
             "-o", "ChallengeResponseAuthentication=no",
             "-o", "PasswordAuthentication=no",
             "localhost", "--", "true"],
            stdin=subprocess.DEVNULL, timeout=10,
        )
    except subprocess.TimeoutExpired:
        res = 1
    if res != 0:
        _ssh_available = False
        pytest.skip("Unable to ssh into localhost without password prompt.")
    _ssh_available = True


_mount_ctr = [0]


def _mount_sshfs(tmpdir, extra_opts=None):
    """Helper to mount sshfs with custom options. Returns (mount_process, mnt_dir, src_dir)."""
    _check_ssh_localhost()
    _mount_ctr[0] += 1
    mnt_dir = str(tmpdir.mkdir(f"mnt{_mount_ctr[0]}"))
    src_dir = str(tmpdir.mkdir(f"src{_mount_ctr[0]}"))

    cmdline = base_cmdline + [
        pjoin(basename, "sshfs"),
        "-f",
        f"localhost:{src_dir}",
        mnt_dir,
        "-o", "entry_timeout=0",
        "-o", "attr_timeout=0",
    ]
    if extra_opts is not None:
        for opt in extra_opts:
            cmdline += ["-o", opt]

    new_env = dict(os.environ)
    new_env["G_DEBUG"] = "fatal-warnings"

    mount_process = subprocess.Popen(cmdline, env=new_env)
    try:
        wait_for_mount(mount_process, mnt_dir)
    except Exception:
        cleanup(mount_process, mnt_dir)
        raise
    return mount_process, mnt_dir, src_dir
