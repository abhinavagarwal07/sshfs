#!/usr/bin/env python3

if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main([__file__] + sys.argv[1:]))

import ctypes
import ctypes.util
import os
import pytest
import random
import sys
from contextlib import contextmanager, suppress
from os.path import join as pjoin
from util import fuse_test_marker, mounted, os_create

# The struct dirent below is the one from glibc on 64-bit Linux.
LINUX_LP64 = sys.platform.startswith("linux") and ctypes.sizeof(ctypes.c_void_p) == 8

pytestmark = [
    fuse_test_marker(),
    pytest.mark.skipif(not LINUX_LP64, reason="needs the Linux LP64 struct dirent"),
]

# sshfs hands FUSE the whole listing and libfuse slices it by offset: once
# filled, a non-zero offset is served from libfuse's copy of the listing,
# offset 0 (first read, rewinddir) asks sshfs again. These tests drive that
# through opendir/readdir/telldir/seekdir/rewinddir on a big directory.
N_FILES = 5000
PARTIAL = 1500  # entries read before telldir()/rewinddir()
DT_UNKNOWN = 0
DT_REG = 8

# dir_cache goes through cache.c, sync_readdir picks the SFTP reader
variants = pytest.mark.parametrize(
    "dir_cache, sync_readdir",
    [
        pytest.param("yes", False, id="dir_cache=yes-sync_readdir=off"),
        pytest.param("yes", True, id="dir_cache=yes-sync_readdir=on"),
        pytest.param("no", False, id="dir_cache=no-sync_readdir=off"),
        pytest.param("no", True, id="dir_cache=no-sync_readdir=on"),
    ],
)

if LINUX_LP64:

    class Dirent(ctypes.Structure):
        _fields_ = [
            ("d_ino", ctypes.c_uint64),
            ("d_off", ctypes.c_int64),
            ("d_reclen", ctypes.c_ushort),
            ("d_type", ctypes.c_ubyte),
            ("d_name", ctypes.c_char * 256),
        ]

    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    libc.opendir.argtypes = [ctypes.c_char_p]
    libc.opendir.restype = ctypes.c_void_p
    libc.readdir.argtypes = [ctypes.c_void_p]
    libc.readdir.restype = ctypes.POINTER(Dirent)
    libc.telldir.argtypes = [ctypes.c_void_p]
    libc.telldir.restype = ctypes.c_long
    libc.seekdir.argtypes = [ctypes.c_void_p, ctypes.c_long]
    libc.seekdir.restype = None
    libc.rewinddir.argtypes = [ctypes.c_void_p]
    libc.rewinddir.restype = None
    libc.closedir.argtypes = [ctypes.c_void_p]
    libc.closedir.restype = ctypes.c_int


@contextmanager
def opendir(path):
    dirp = libc.opendir(os.fsencode(path))
    if not dirp:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), path)
    try:
        yield dirp
    finally:
        if libc.closedir(dirp):
            raise OSError(ctypes.get_errno(), "closedir failed", path)


def readdir(dirp, limit=None):
    """Read up to *limit* entries (all that are left if None) as (name, d_type)"""

    entries = []
    while limit is None or len(entries) < limit:
        ctypes.set_errno(0)
        ent = libc.readdir(dirp)
        if not ent:
            err = ctypes.get_errno()
            if err:
                raise OSError(err, os.strerror(err))
            break
        entries.append((os.fsdecode(ent.contents.d_name), ent.contents.d_type))
    return entries


def names_of(entries):
    return [name for name, _ in entries]


def check_listing(entries, expected):
    names = names_of(entries)
    assert len(names) == len(set(names)), "duplicate names"
    assert set(names) == expected
    assert all(t in (DT_UNKNOWN, DT_REG) for n, t in entries if n not in (".", ".."))


@pytest.fixture(scope="module")
def big_dir(tmp_path_factory):
    """(path, expected) of a directory with N_FILES empty files, built once.

    expected is the set of names in a complete listing, "." and ".." included.
    The tests only read it; the one that adds a file removes it again.
    """

    rng = random.Random(0x5E7D1A)
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789_-"
    path = str(tmp_path_factory.mktemp("big"))
    names = []
    for i in range(N_FILES):
        prefix = f"f{i:05d}_"
        length = rng.randint(90, 110)
        names.append(prefix + "".join(rng.choices(alphabet, k=length - len(prefix))))
        os_create(pjoin(path, names[-1]))
    return path, frozenset(names) | {".", ".."}


@contextmanager
def mounted_big_dir(tmpdir, big_dir, dir_cache, sync_readdir):
    opts = [f"dir_cache={dir_cache}"] + (["sync_readdir"] if sync_readdir else [])
    with mounted(tmpdir, opts, src_dir=big_dir[0]) as (mnt_dir, _):
        yield mnt_dir


@variants
def test_large_directory_listing(tmpdir, big_dir, dir_cache, sync_readdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)

    _, expected = big_dir
    want = sorted(expected - {".", ".."})
    with mounted_big_dir(tmpdir, big_dir, dir_cache, sync_readdir) as mnt_dir:
        assert sorted(os.listdir(mnt_dir)) == want

        # second pass: normally a cache hit with dir_cache=yes
        with os.scandir(mnt_dir) as it:
            entries = list(it)
        assert sorted(entry.name for entry in entries) == want
        for entry in entries[::50]:
            assert entry.is_file(follow_symlinks=False)


@variants
def test_seekdir_telldir_same_handle(tmpdir, big_dir, dir_cache, sync_readdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)

    _, expected = big_dir
    with mounted_big_dir(tmpdir, big_dir, dir_cache, sync_readdir) as mnt_dir:
        with opendir(mnt_dir) as dirp:
            head = readdir(dirp, PARTIAL)
            pos = libc.telldir(dirp)
            tail = readdir(dirp)
            libc.seekdir(dirp, pos)
            assert names_of(readdir(dirp)) == names_of(tail)

        assert len(head) == PARTIAL
        check_listing(head + tail, expected)


@variants
def test_rewinddir_after_partial_read(tmpdir, big_dir, dir_cache, sync_readdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)

    src_dir, expected = big_dir
    new_name = "new_file_after_rewind"
    with mounted_big_dir(tmpdir, big_dir, dir_cache, sync_readdir) as mnt_dir:
        try:
            with opendir(mnt_dir) as dirp:
                assert len(readdir(dirp, PARTIAL)) == PARTIAL
                libc.rewinddir(dirp)
                check_listing(readdir(dirp), expected)

                # The handle is at EOF. Creating a file through the mount
                # invalidates the dir cache, so the rewind has to ask the
                # server again.
                os_create(pjoin(mnt_dir, new_name))
                libc.rewinddir(dirp)
                check_listing(readdir(dirp), expected | {new_name})
        finally:
            # src_dir is shared with the other tests
            with suppress(FileNotFoundError):
                os.unlink(pjoin(src_dir, new_name))


@variants
def test_seekdir_fresh_handle(tmpdir, big_dir, dir_cache, sync_readdir, capfd):
    """Linux/libfuse regression test; telldir cookies from another stream are
    not portable per POSIX.

    seekdir() on a stream that was never read makes the first FUSE_READDIR (or
    READDIRPLUS) carry a non-zero offset. 3.7.6 aborted on assert(offset == 0)
    for that, in cache_readdir() with dir_cache=yes and in sshfs_readdir()
    with dir_cache=no (fixed by upstream PR 379). Fails there in all variants.
    """

    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)

    _, expected = big_dir
    with mounted_big_dir(tmpdir, big_dir, dir_cache, sync_readdir) as mnt_dir:
        with opendir(mnt_dir) as first:
            head = readdir(first, PARTIAL)
            pos = libc.telldir(first)
            assert pos > 0
            with opendir(mnt_dir) as second:
                libc.seekdir(second, pos)
                names = names_of(readdir(second))
            tail = readdir(first)

        # libfuse skips `pos` entries of whatever listing it just got, so only
        # the count is the same: the server may order two OPENDIRs differently.
        assert len(set(names)) == len(names)
        assert set(names) <= expected
        assert len(names) == len(tail)
        check_listing(head + tail, expected)

        # sshfs is still up
        assert sorted(os.listdir(mnt_dir)) == sorted(expected - {".", ".."})
