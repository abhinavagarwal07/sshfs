#!/usr/bin/env python3

if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main([__file__] + sys.argv[1:]))

import errno
import os
import shutil
import subprocess
import pytest
from contextlib import contextmanager
from os.path import join as pjoin
from util import fuse_test_marker, mounted

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

            # the refused operations left the visible state alone
            assert os.listdir(pjoin(mnt_dir, "locked_dir")) == ["victim"]
            with open(pjoin(mnt_dir, "readonly"), "rb") as fh:
                assert fh.read() == b"readonly data"
        finally:
            # so that the temporary directory can be removed
            os.chmod(locked_dir, 0o700)
            os.chmod(secret, 0o600)


@contextmanager
def small_tmpfs(path):
    """A 1 MB tmpfs at *path*, owned by us. Needs root or passwordless sudo."""
    if os.getuid() == 0:
        sudo = []
    elif shutil.which("sudo") and subprocess.call(["sudo", "-n", "true"]) == 0:
        sudo = ["sudo", "-n"]
    else:
        pytest.skip("needs root or passwordless sudo to mount a tmpfs")
    opts = f"size=1M,uid={os.getuid()},gid={os.getgid()},mode=0755"
    subprocess.check_call(
        sudo + ["mount", "-t", "tmpfs", "-o", opts, "tmpfs", path], timeout=30
    )
    try:
        yield
    finally:
        # lazy, in case the ssh processes are still on their way out
        subprocess.check_call(sudo + ["umount", "-l", path], timeout=30)
        assert not os.path.ismount(path)


def test_disk_full_enospc(tmpdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)

    full_dir = str(tmpdir.mkdir("tmpfs"))
    on_tmpfs = mounted(tmpdir, ["dir_cache=no"], full_dir)
    with small_tmpfs(full_dir), on_tmpfs as (mnt_dir, _):
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
        # sshfs turns a failed SFTP write into EIO. Make sure the
        # file system really is full, not just that a write failed.
        assert error.errno in (errno.ENOSPC, errno.EIO), error
        with pytest.raises(OSError) as exc_info:
            with open(pjoin(full_dir, "probe"), "wb") as fh:
                fh.write(b"\0" * 4096)
                fh.flush()
        assert exc_info.value.errno == errno.ENOSPC

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
