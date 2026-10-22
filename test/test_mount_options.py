#!/usr/bin/env python3

if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main([__file__] + sys.argv[1:]))

import grp
import os
import pwd
import stat
import pytest
from os.path import join as pjoin
from util import fuse_test_marker, mounted
from test_sshfs import (
    name_generator,
    tst_append,
    tst_chmod,
    tst_create,
    tst_fsync,
    tst_open_read,
    tst_open_write,
    tst_rename,
    tst_rename_over,
    tst_seek,
    tst_truncate_fd,
    tst_truncate_path,
)

pytestmark = fuse_test_marker()


# Mount options that switch to a different code path in sshfs. The checks are
# the generic file operations from test_sshfs.py: whatever the option does
# internally, what the user sees must not change.


@pytest.mark.parametrize(
    "workaround",
    ["truncate", "fstat", "renamexdev", "buflimit", "truncate:fstat:renamexdev"],
)
def test_workaround(tmpdir, capfd, workaround):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    with mounted(tmpdir, [f"workaround={workaround}"]) as (mnt_dir, src_dir):
        tst_truncate_path(mnt_dir)
        tst_truncate_fd(mnt_dir)
        tst_fsync(src_dir, mnt_dir)
        tst_rename(mnt_dir)
        tst_rename_over(mnt_dir)
        tst_chmod(mnt_dir)


def test_createmode_workaround(tmpdir, capfd):
    # With this workaround files are created with mode 0 and the mode is not
    # set until a chmod, so the generic checks can't be used here.
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    with mounted(tmpdir, ["workaround=createmode"]) as (mnt_dir, src_dir):
        path = pjoin(mnt_dir, name_generator())
        with open(path, "wb") as fh:
            fh.write(b"createmode test\n")
        assert stat.S_IMODE(os.stat(path).st_mode) == 0

        os.chmod(path, 0o644)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o644
        with open(path, "rb") as fh:
            assert fh.read() == b"createmode test\n"
        os.truncate(path, 4)
        with open(path, "rb") as fh:
            assert fh.read() == b"crea"
        os.unlink(path)

        tst_chmod(mnt_dir)


def test_idmap_user(tmpdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    with mounted(tmpdir, ["idmap=user"]) as (mnt_dir, src_dir):
        name = name_generator()
        with open(pjoin(src_dir, name), "wb") as fh:
            fh.write(b"idmap user test")

        st = os.stat(pjoin(mnt_dir, name))
        assert st.st_uid == os.getuid()
        assert st.st_gid == os.getgid()


def test_idmap_file(tmpdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    try:
        user = pwd.getpwuid(os.getuid()).pw_name
        group = grp.getgrgid(os.getgid()).gr_name
    except KeyError:
        pytest.skip("current user or group has no name")

    # name:id, where id is the owner as the server reports it. On localhost
    # that is us, so this maps our own ids to themselves, but a file that is
    # not listed would make the stat fail (nomap=error is the default).
    uidfile = tmpdir.join("uidmap")
    uidfile.write(f"{user}:{os.getuid()}\n")
    uidfile.chmod(0o600)
    gidfile = tmpdir.join("gidmap")
    gidfile.write(f"{group}:{os.getgid()}\n")
    gidfile.chmod(0o600)

    opts = ["idmap=file", f"uidfile={uidfile}", f"gidfile={gidfile}"]
    with mounted(tmpdir, opts) as (mnt_dir, src_dir):
        name = name_generator()
        with open(pjoin(src_dir, name), "wb") as fh:
            fh.write(b"idmap file test")

        st = os.stat(pjoin(mnt_dir, name))
        assert st.st_uid == os.getuid()
        assert st.st_gid == os.getgid()


def test_sshfs_sync(tmpdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    with mounted(tmpdir, ["sshfs_sync"]) as (mnt_dir, src_dir):
        tst_open_write(src_dir, mnt_dir)
        tst_append(src_dir, mnt_dir)
        tst_truncate_path(mnt_dir)
        tst_fsync(src_dir, mnt_dir)


def test_no_readahead(tmpdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    with mounted(tmpdir, ["no_readahead"]) as (mnt_dir, src_dir):
        tst_open_read(src_dir, mnt_dir)
        tst_seek(src_dir, mnt_dir)
        tst_truncate_path(mnt_dir)


def test_small_max_rw(tmpdir, capfd):
    # The test file is much larger than 4096, so every read and write is split
    # into many requests.
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    with mounted(tmpdir, ["max_read=4096", "max_write=4096"]) as (mnt_dir, src_dir):
        tst_open_read(src_dir, mnt_dir)
        tst_open_write(src_dir, mnt_dir)
        tst_append(src_dir, mnt_dir)
        tst_seek(src_dir, mnt_dir)
        tst_truncate_path(mnt_dir)


def test_delay_connect(tmpdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    with mounted(tmpdir, ["delay_connect"]) as (mnt_dir, src_dir):
        tst_create(mnt_dir)
        tst_open_read(src_dir, mnt_dir)
        tst_open_write(src_dir, mnt_dir)
