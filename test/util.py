#!/usr/bin/env python3
import subprocess
import pytest
import os
import stat
import re
import time
import shutil
from os.path import join as pjoin
from contextlib import contextmanager


def _find_fusermount():
    """Return the name of the FUSE 3 fusermount, or None

    libfuse 3 installs fusermount3; some distros also have a fusermount, which
    may belong to FUSE 2 and is no use here.
    """
    if shutil.which("fusermount3"):
        return "fusermount3"
    if shutil.which("fusermount"):
        try:
            out = subprocess.run(
                ["fusermount", "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                timeout=10,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        if re.search(r"version:\s*3\.", out):
            return "fusermount"
    return None


FUSERMOUNT_BIN = _find_fusermount() or "fusermount3"

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
        [FUSERMOUNT_BIN, "-z", "-u", mnt_dir],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    mount_process.terminate()
    try:
        mount_process.wait(1)
    except subprocess.TimeoutExpired:
        mount_process.kill()


def umount(mount_process, mnt_dir):
    subprocess.check_call([FUSERMOUNT_BIN, "-z", "-u", mnt_dir])
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


def require_fuse():
    """Return True if tests that can't run must fail instead of skip

    CI sets SSHFS_TEST_REQUIRE_FUSE so that a runner without working FUSE
    fails instead of reporting green with every test skipped.
    """
    return os.environ.get("SSHFS_TEST_REQUIRE_FUSE", "no").lower().strip() not in (
        "",
        "no",
        "false",
        "0",
    )


def fuse_test_marker():
    """Return a pytest.marker that indicates FUSE availability

    If system/user/environment does not support FUSE, return
    a `pytest.mark.skip` object with more details. If FUSE is
    supported, return `pytest.mark.uses_fuse()`.
    """

    def skip(reason: str):
        if require_fuse():
            pytest.fail(f"FUSE tests required but unusable: {reason}", pytrace=False)
        return pytest.mark.skip(reason=reason)

    fusermount_path = shutil.which(FUSERMOUNT_BIN)
    if not fusermount_path:
        return skip("Can't find a FUSE 3 fusermount executable")

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
