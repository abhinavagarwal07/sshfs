#!/usr/bin/env python3

if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main([__file__] + sys.argv[1:]))

import errno
import os
from os.path import join as pjoin

import pytest

from test_reconnect_cache import (
    _mounted_with_recorded_ssh,
    _reconnect,
    _register_expected_disconnect_output,
)
from util import fuse_test_marker


pytestmark = fuse_test_marker()


@pytest.mark.parametrize(
    "extra_opts",
    [
        pytest.param([], id="async-readdir-cache"),
        pytest.param(["sync_readdir"], id="sync-readdir-cache"),
        pytest.param(["dir_cache=no"], id="dir-cache-off"),
    ],
)
def test_stale_directory_handle_fails_after_reconnect(tmpdir, capfd, extra_opts):
    # Python's os.listdir(fd) rewinds the descriptor after listing it, so the
    # second listing below is a new enumeration on the old directory handle.
    assert os.listdir in os.supports_fd

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
        with open(pjoin(src_dir, "probe"), "wb") as fh:
            fh.write(b"probe")

        dirfd = os.open(mnt_subdir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            assert sorted(os.listdir(dirfd)) == ["old"]
            os.lseek(dirfd, 0, os.SEEK_SET)

            _reconnect(pidfile, pjoin(mnt_dir, "probe"))

            # The handle belongs to the old session; it must not be replayed
            with pytest.raises(OSError) as exc_info:
                os.listdir(dirfd)
            assert exc_info.value.errno in (errno.EIO, errno.ENOTCONN)
        finally:
            os.close(dirfd)
