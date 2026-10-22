#!/usr/bin/env python3

if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main([__file__] + sys.argv[1:]))

import hashlib
import multiprocessing
import os
import random
import pytest
from os.path import join as pjoin
from util import fuse_test_marker, mounted
from test_sshfs import name_generator

pytestmark = fuse_test_marker()


# File names are handled as bytes, so the result doesn't depend on the locale
# the tests happen to run in.
SPECIAL_NAMES = [
    "file with spaces",
    "file'single_quote",
    'file"double_quote',
    "file\\backslash",
    "file#hash",
    "file%percent",
    "file;semicolon$dollar`backtick",
    "star*and?question",
    "-leading-dash",
    "file\nwith_newline",
    "x" * 255,
]

UNICODE_NAMES = [
    "café",  # e with acute, precomposed (NFC)
    "café",  # e followed by a combining acute (NFD)
    "世界",  # CJK
    "\U0001f4c1folder",  # 4-byte UTF-8 sequence
]


def assert_same_data(actual, expected, what):
    """assert actual == expected, without dumping the data on failure"""
    if actual == expected:
        return
    n = min(len(actual), len(expected))
    first = next((i for i in range(n) if actual[i] != expected[i]), n)
    pytest.fail(
        f"{what}: first difference at byte {first}, "
        f"got {len(actual)} bytes, expected {len(expected)}"
    )


def check_name_lifecycle(mnt_dir, name):
    """Create, list, read, stat and unlink a file called *name*"""
    dir_b = os.fsencode(mnt_dir)
    name_b = name.encode("utf-8")
    path = dir_b + b"/" + name_b
    content = b"content for " + name_b

    with open(path, "wb") as fh:
        fh.write(content)
    assert name_b in os.listdir(dir_b), f"{name!r} not found in listdir"

    with open(path, "rb") as fh:
        assert fh.read() == content, f"read-back mismatch for {name!r}"
    assert os.stat(path).st_size == len(content), f"size mismatch for {name!r}"

    os.unlink(path)
    assert name_b not in os.listdir(dir_b), f"{name!r} still listed after unlink"


def test_special_filenames(tmpdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    with mounted(tmpdir) as (mnt_dir, src_dir):
        for name in SPECIAL_NAMES:
            check_name_lifecycle(mnt_dir, name)


def test_unicode_filenames(tmpdir, capfd):
    # Each name is handled on its own. This is about sshfs passing arbitrary
    # UTF-8 through unchanged, not about normalization.
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    with mounted(tmpdir) as (mnt_dir, src_dir):
        for name in UNICODE_NAMES:
            check_name_lifecycle(mnt_dir, name)


def test_sparse_file(tmpdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    with mounted(tmpdir) as (mnt_dir, src_dir):
        fullname = pjoin(mnt_dir, name_generator())
        block = 4096
        one_mb = 1024 * 1024

        # 4 KB at offset 0 and another at 1 MB, leaving a hole in between
        fd = os.open(fullname, os.O_CREAT | os.O_RDWR | os.O_TRUNC)
        try:
            os.write(fd, b"A" * block)
            os.lseek(fd, one_mb, os.SEEK_SET)
            os.write(fd, b"B" * block)
        finally:
            os.close(fd)

        expected_size = one_mb + block
        assert os.stat(fullname).st_size == expected_size

        with open(fullname, "rb") as fh:
            assert fh.read(block) == b"A" * block
            assert_same_data(fh.read(one_mb - block), b"\0" * (one_mb - block), "hole")
            assert fh.read(block) == b"B" * block

        # Extending with truncate reads back as zeros as well
        os.truncate(fullname, 2 * one_mb)
        assert os.stat(fullname).st_size == 2 * one_mb
        with open(fullname, "rb") as fh:
            fh.seek(expected_size)
            assert_same_data(
                fh.read(),
                b"\0" * (2 * one_mb - expected_size),
                "region past the old EOF",
            )

        os.unlink(fullname)


def test_large_file(tmpdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    chunk_size = 128 * 1024
    num_chunks = 512  # 64 MB
    seek_size = chunk_size + 1  # not a multiple of the chunk size
    num_seeks = 20

    def make_chunk(index):
        return random.Random(0xDEADBEEF + index).randbytes(chunk_size)

    with mounted(tmpdir) as (mnt_dir, src_dir):
        filename = pjoin(mnt_dir, name_generator())

        hasher = hashlib.sha256()
        with open(filename, "wb") as fh:
            for i in range(num_chunks):
                chunk = make_chunk(i)
                fh.write(chunk)
                hasher.update(chunk)
        expected_hash = hasher.digest()
        assert os.stat(filename).st_size == chunk_size * num_chunks

        # Sequential read back
        hasher = hashlib.sha256()
        with open(filename, "rb") as fh:
            while True:
                buf = fh.read(chunk_size)
                if not buf:
                    break
                hasher.update(buf)
        assert hasher.digest() == expected_hash, "SHA-256 mismatch on sequential read"

        # Reads at offsets that cross chunk boundaries
        total_size = chunk_size * num_chunks
        seek_rng = random.Random(42)
        max_block = total_size // seek_size - 1
        with open(filename, "rb") as fh:
            for _ in range(num_seeks):
                offset = seek_rng.randint(0, max_block) * seek_size
                fh.seek(offset)
                buf = fh.read(seek_size)
                assert len(buf) == seek_size, f"short read at offset {offset}"

                expected = bytearray()
                pos = offset
                remaining = seek_size
                while remaining > 0:
                    lo = pos % chunk_size
                    take = min(chunk_size - lo, remaining)
                    expected += make_chunk(pos // chunk_size)[lo : lo + take]
                    pos += take
                    remaining -= take
                assert_same_data(buf, expected, f"read at offset {offset}")

        os.unlink(filename)


def test_random_file_ops(tmpdir, capfd):
    """Random writes, truncates and reads, checked against a bytearray"""
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    initial_size = 512 * 1024
    max_size = 2 * initial_size
    max_rw_len = 8 * 1024
    iterations = 500
    rng = random.Random(0xDEADBEEF)

    with mounted(tmpdir) as (mnt_dir, src_dir):
        filename = pjoin(mnt_dir, name_generator())
        with open(filename, "wb") as fh:
            fh.write(b"\0" * initial_size)
        truth = bytearray(initial_size)

        for _ in range(iterations):
            op = rng.choice(["write", "truncate", "read"])
            cur = len(truth)

            if op == "write":
                offset = rng.randint(0, cur)
                max_len = min(max_rw_len, max_size - offset)
                if max_len <= 0:
                    continue
                data = rng.randbytes(rng.randint(1, max_len))
                if offset + len(data) > cur:
                    truth.extend(b"\0" * (offset + len(data) - cur))
                with open(filename, "r+b") as fh:
                    fh.seek(offset)
                    fh.write(data)
                truth[offset : offset + len(data)] = data

            elif op == "truncate":
                new_size = rng.randint(0, max_size)
                os.truncate(filename, new_size)
                if new_size > cur:
                    truth.extend(b"\0" * (new_size - cur))
                else:
                    del truth[new_size:]

            else:
                if cur == 0:
                    continue
                offset = rng.randint(0, cur - 1)
                length = rng.randint(1, min(max_rw_len, cur - offset))
                with open(filename, "rb") as fh:
                    fh.seek(offset)
                    buf = fh.read(length)
                assert_same_data(
                    buf,
                    truth[offset : offset + length],
                    f"read at offset={offset} length={length}",
                )

        assert os.stat(filename).st_size == len(truth)
        with open(filename, "rb") as fh:
            assert_same_data(fh.read(), truth, "final contents")
        os.unlink(filename)


@pytest.mark.parametrize("max_conns", [1, 3])
def test_overlapping_writes(tmpdir, capfd, max_conns):
    """Writes through two descriptors of one file land in the order issued"""
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    with mounted(tmpdir, [f"max_conns={max_conns}"]) as (mnt_dir, src_dir):
        path = pjoin(mnt_dir, name_generator())
        with open(path, "wb") as fh:
            fh.write(b"\0" * 1024)

        fd1 = os.open(path, os.O_WRONLY)
        fd2 = os.open(path, os.O_WRONLY)
        try:
            os.pwrite(fd1, b"X" * 512, 0)
            os.pwrite(fd2, b"Y" * 512, 0)
            os.pwrite(fd1, b"Z" * 256, 0)
        finally:
            os.close(fd1)
            os.close(fd2)

        expected = b"Z" * 256 + b"Y" * 256 + b"\0" * 512
        with open(path, "rb") as fh:
            assert fh.read() == expected
        with open(pjoin(src_dir, os.path.basename(path)), "rb") as fh:
            assert fh.read() == expected


def test_dual_mount(tmpdir, capfd):
    """Two sshfs instances on the same remote directory see each other's changes"""
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    shared = str(tmpdir.mkdir("shared_src"))
    opts = ["dir_cache=no"]

    with mounted(tmpdir, opts, shared) as (mnt1, _):
        with mounted(tmpdir, opts, shared) as (mnt2, _):
            with open(pjoin(mnt1, "shared_file"), "wb") as fh:
                fh.write(b"written via mnt1")
            assert "shared_file" in os.listdir(mnt2)
            with open(pjoin(mnt2, "shared_file"), "rb") as fh:
                assert fh.read() == b"written via mnt1"

            with open(pjoin(mnt2, "shared_file"), "wb") as fh:
                fh.write(b"overwritten via mnt2")
            with open(pjoin(mnt1, "shared_file"), "rb") as fh:
                assert fh.read() == b"overwritten via mnt2"

            os.unlink(pjoin(mnt2, "shared_file"))
            assert not os.path.exists(pjoin(mnt1, "shared_file"))
            assert os.listdir(mnt1) == []


def _writer_data(idx):
    return random.Random(idx).randbytes(256 * 1024)


# Needs to be at module level so that it can be pickled for the pool
def _parallel_writer(args):
    idx, mnt_dir = args
    data = _writer_data(idx)
    path = pjoin(mnt_dir, f"parallel_{idx}.bin")
    with open(path, "wb") as fh:
        for off in range(0, len(data), 8192):
            fh.write(data[off : off + 8192])
    with open(path, "rb") as fh:
        assert fh.read() == data, f"read-back mismatch in writer {idx}"


def test_parallel_writers(tmpdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    workers = 8
    with mounted(tmpdir, ["max_conns=4"]) as (mnt_dir, src_dir):
        ctx = multiprocessing.get_context("fork")
        with ctx.Pool(workers) as pool:
            pool.map(_parallel_writer, [(i, mnt_dir) for i in range(workers)])

        # Check from the server side too, so a cache can't hide a bad write
        for i in range(workers):
            with open(pjoin(src_dir, f"parallel_{i}.bin"), "rb") as fh:
                assert_same_data(fh.read(), _writer_data(i), f"file {i} on the server")
