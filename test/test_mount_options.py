#!/usr/bin/env python3

if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main([__file__] + sys.argv[1:]))

import errno
import grp
import os
import pwd
import stat
import pytest
from os.path import join as pjoin
from util import fuse_test_marker, mounted
from test_sshfs import (
    name_generator,
    tst_access,
    tst_append,
    tst_chmod,
    tst_create,
    tst_fsync,
    tst_mkdir,
    tst_mkdir_exist,
    tst_open_read,
    tst_open_unlink,
    tst_open_write,
    tst_open_writeonly_read,
    tst_readdir,
    tst_readdir_repeated,
    tst_rename,
    tst_rename_open_release,
    tst_rename_over,
    tst_rename_sibling,
    tst_seek,
    tst_statvfs,
    tst_truncate_fd,
    tst_truncate_path,
    tst_utimens,
    tst_utimens_now,
    tst_utimens_omit,
)

pytestmark = fuse_test_marker()


# Mount options that switch to a different code path in sshfs. The checks are
# the generic file operations from test_sshfs.py: whatever the option does
# internally, what the user sees must not change.


@pytest.mark.parametrize("workaround", ["truncate", "fstat"])
def test_workaround(tmpdir, capfd, workaround):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    with mounted(tmpdir, [f"workaround={workaround}"]) as (mnt_dir, src_dir):
        tst_truncate_path(mnt_dir)
        tst_truncate_fd(mnt_dir)
        tst_fsync(src_dir, mnt_dir)
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
    # On localhost the server's ids are ours already, so this can only show
    # that a mount with the option works.
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    with mounted(tmpdir, ["idmap=user"]) as (mnt_dir, src_dir):
        name = name_generator()
        with open(pjoin(src_dir, name), "wb") as fh:
            fh.write(b"idmap user test")

        st = os.stat(pjoin(mnt_dir, name))
        assert st.st_uid == os.getuid()
        assert st.st_gid == os.getgid()


def test_idmap_file(tmpdir, capfd):
    """Our files, as the server reports them, show up as owned by nobody"""
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    nobody = pwd.getpwnam("nobody")
    for name in ("nogroup", "nobody"):
        try:
            nogroup = grp.getgrnam(name)
            break
        except KeyError:
            pass
    else:
        pytest.fail("no nogroup or nobody group")

    # name:id, where id is the owner as the server reports it (us, on
    # localhost) and name is the local user or group it is shown as
    uidfile = tmpdir.join("uidmap")
    uidfile.write(f"{nobody.pw_name}:{os.getuid()}\n")
    uidfile.chmod(0o600)
    gidfile = tmpdir.join("gidmap")
    gidfile.write(f"{nogroup.gr_name}:{os.getgid()}\n")
    gidfile.chmod(0o600)

    opts = ["idmap=file", f"uidfile={uidfile}", f"gidfile={gidfile}"]
    with mounted(tmpdir, opts) as (mnt_dir, src_dir):
        name = name_generator()
        with open(pjoin(src_dir, name), "wb") as fh:
            fh.write(b"idmap file test")
        assert os.stat(pjoin(src_dir, name)).st_uid == os.getuid()

        st = os.stat(pjoin(mnt_dir, name))
        assert st.st_uid == nobody.pw_uid
        assert st.st_gid == nogroup.gr_gid


def test_sshfs_sync(tmpdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    with mounted(tmpdir, ["sshfs_sync"]) as (mnt_dir, src_dir):
        tst_open_write(src_dir, mnt_dir)
        tst_append(src_dir, mnt_dir)
        tst_truncate_path(mnt_dir)
        tst_fsync(src_dir, mnt_dir)


def test_no_readahead(tmpdir, capfd):
    # sshfs_init() turns on sync_read whenever the kernel offers async reads,
    # which is always the case on Linux, so this only checks that the option
    # is accepted.
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    with mounted(tmpdir, ["no_readahead"]) as (mnt_dir, src_dir):
        tst_open_read(src_dir, mnt_dir)


def test_small_max_rw(tmpdir, capfd):
    # The test file is much larger than 4096, so the whole-file reads and
    # writes are split into many requests.
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    with mounted(tmpdir, ["max_read=4096", "max_write=4096"]) as (mnt_dir, src_dir):
        tst_open_read(src_dir, mnt_dir)
        tst_open_write(src_dir, mnt_dir)
        tst_append(src_dir, mnt_dir)
        tst_seek(src_dir, mnt_dir)
        tst_truncate_path(mnt_dir)


def mount_listed(mnt_dir):
    """Is mnt_dir a mount point? Unlike os.path.ismount() this doesn't touch it."""
    parent, name = os.path.split(mnt_dir)
    mnt_dir = pjoin(os.path.realpath(parent), name)  # realpath() would stat it
    with open("/proc/self/mountinfo") as fh:
        return any(line.split()[4] == mnt_dir for line in fh)


def test_delay_connect(tmpdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    # ssh is started through a wrapper that logs each start
    log = str(tmpdir.join("ssh-starts"))
    wrapper = tmpdir.join("ssh-wrapper")
    wrapper.write(f"#!/bin/sh\necho started >> '{log}'\nexec ssh \"$@\"\n")
    wrapper.chmod(0o755)

    opts = ["delay_connect", f"ssh_command={wrapper}"]
    with mounted(tmpdir, opts, ready=mount_listed) as (mnt_dir, src_dir):
        assert not os.path.exists(log), "connected before the first access"
        tst_create(mnt_dir)
        assert os.path.exists(log), "no connection after the first access"
        tst_open_read(src_dir, mnt_dir)
        tst_open_write(src_dir, mnt_dir)


def test_default_options(tmpdir, capfd):
    """A mount with no options, which is what most people run

    The kernel and sshfs both cache entries and attributes, and symlinks are
    contained. Zero kernel timeouts, as in the other tests, don't expose every
    change made behind the mount either, so only operations through the mount
    and names the mount hasn't seen yet are used.
    """
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    with mounted(tmpdir, zero_timeouts=False) as (mnt_dir, src_dir):
        tst_create(mnt_dir)
        tst_mkdir(mnt_dir)
        tst_mkdir_exist(mnt_dir)
        tst_open_read(src_dir, mnt_dir)
        tst_open_write(src_dir, mnt_dir)
        tst_append(src_dir, mnt_dir)
        tst_seek(src_dir, mnt_dir)
        tst_fsync(src_dir, mnt_dir)
        tst_truncate_path(mnt_dir)
        tst_truncate_fd(mnt_dir)
        tst_open_unlink(mnt_dir)
        tst_chmod(mnt_dir)
        tst_access(mnt_dir)
        tst_rename(mnt_dir)
        tst_rename_over(mnt_dir)
        tst_rename_sibling(mnt_dir)
        tst_rename_open_release(mnt_dir)
        tst_readdir_repeated(mnt_dir)
        tst_readdir(src_dir, mnt_dir)
        tst_open_writeonly_read(mnt_dir)
        tst_statvfs(src_dir, mnt_dir)
        # SFTP only has one second resolution
        tst_utimens(mnt_dir, tol=1)
        tst_utimens_now(mnt_dir)
        tst_utimens_omit(mnt_dir)

        # A relative link inside the directory works under the default
        # containment. Of the refused ones, only absolute targets and ".."
        # above the link's directory or after a directory are checked.
        os.mkdir(pjoin(mnt_dir, "sub"))
        with open(pjoin(mnt_dir, "sub", "target"), "wb") as fh:
            fh.write(b"through a link")
        os.symlink("sub/target", pjoin(mnt_dir, "link"))
        assert os.readlink(pjoin(mnt_dir, "link")) == "sub/target"
        with open(pjoin(mnt_dir, "link"), "rb") as fh:
            assert fh.read() == b"through a link"

        refused = {
            "absolute": "/etc/passwd",
            "above": "../outside",
            "sub/deeper": "../../outside",
            "sub/interleaved": "sub/../target",
        }
        for name, target in refused.items():
            os.symlink(target, pjoin(src_dir, name))
            for op in (os.readlink, os.stat, open):
                with pytest.raises(OSError) as exc_info:
                    op(pjoin(mnt_dir, name))
                assert exc_info.value.errno == errno.EPERM, (name, op)
