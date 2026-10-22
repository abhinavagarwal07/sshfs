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
    try:
        subprocess.call(
            [FUSERMOUNT_BIN, "-z", "-u", mnt_dir],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        pass
    finally:
        # give it time to exit by itself, SIGTERM cuts its shutdown short
        try:
            mount_process.wait(5)
        except subprocess.TimeoutExpired:
            mount_process.terminate()
            try:
                mount_process.wait(1)
            except subprocess.TimeoutExpired:
                mount_process.kill()
                mount_process.wait()


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
    a `pytest.mark.skip` object with more details, or fail if
    require_fuse() is true. If FUSE is supported, return
    `pytest.mark.uses_fuse()`.
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


SSH_LOCALHOST = [
    "ssh", "-o", "StrictHostKeyChecking=no",
    "-o", "KbdInteractiveAuthentication=no",
    "-o", "ChallengeResponseAuthentication=no",
    "-o", "PasswordAuthentication=no",
    "localhost", "--",
]


def _check_ssh_localhost():
    try:
        res = subprocess.call(
            SSH_LOCALHOST + ["true"], stdin=subprocess.DEVNULL, timeout=10
        )
    except subprocess.TimeoutExpired:
        res = 1
    if res != 0:
        pytest.fail("Unable to ssh into localhost without password prompt.")


_mount_ctr = [0]


def _mount_sshfs(
    tmpdir, extra_opts=None, src_dir=None, zero_timeouts=True, ready=os.path.ismount
):
    """Helper to mount sshfs with custom options. Returns (mount_process, mnt_dir, src_dir).

    The remote directory is a fresh one under tmpdir unless src_dir is given.
    The kernel's entry and attribute timeouts are set to 0 unless zero_timeouts
    is False. sshfs's own directory and attribute cache is not affected. The
    mount is up when ready(mnt_dir) says so.
    """
    _check_ssh_localhost()
    _mount_ctr[0] += 1
    mnt_dir = str(tmpdir.mkdir(f"mnt{_mount_ctr[0]}"))
    if src_dir is None:
        src_dir = str(tmpdir.mkdir(f"src{_mount_ctr[0]}"))

    cmdline = base_cmdline + [
        pjoin(basename, "sshfs"),
        "-f",
        f"localhost:{src_dir}",
        mnt_dir,
    ]
    if zero_timeouts:
        cmdline += ["-o", "entry_timeout=0", "-o", "attr_timeout=0"]
    if extra_opts:
        for opt in extra_opts:
            cmdline += ["-o", opt]

    new_env = dict(os.environ)
    new_env["G_DEBUG"] = "fatal-warnings"

    mount_process = subprocess.Popen(cmdline, env=new_env)
    try:
        wait_for_mount(mount_process, mnt_dir, ready)
    except BaseException:  # wait_for_mount() fails with pytest.fail()
        cleanup(mount_process, mnt_dir)
        raise
    return mount_process, mnt_dir, src_dir


@contextmanager
def mounted(
    tmpdir, extra_opts=None, src_dir=None, zero_timeouts=True, ready=os.path.ismount
):
    """Mount sshfs, yield (mnt_dir, src_dir) and unmount again afterwards.

    If the body fails (or skips), or the unmount does, cleanup() is called, so
    a failing test doesn't leave an sshfs process behind.
    """
    mount_process, mnt_dir, src_dir = _mount_sshfs(
        tmpdir, extra_opts, src_dir, zero_timeouts, ready
    )
    try:
        yield mnt_dir, src_dir
        umount(mount_process, mnt_dir)
    except BaseException:
        cleanup(mount_process, mnt_dir)
        raise
