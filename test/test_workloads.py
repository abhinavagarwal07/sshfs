#!/usr/bin/env python3

if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main([__file__] + sys.argv[1:]))

import hashlib
import multiprocessing
import os
import random
import shutil
import subprocess
import sys

import pytest

from os.path import join as pjoin

from util import (
    cleanup,
    fuse_test_marker,
    umount,
    _mount_sshfs,
)

pytestmark = fuse_test_marker()

# Path to the sshfs repo root (two levels up from test/)
REPO_ROOT = os.path.abspath(pjoin(os.path.dirname(__file__), ".."))
FIXTURE_HELLO_C = pjoin(os.path.dirname(__file__), "fixtures", "hello_c")


def _no_cache_opts():
    return ["dir_cache=no"]


# ---------------------------------------------------------------------------
# test_git_workflow
# ---------------------------------------------------------------------------

def _ensure_fixture_git_repo():
    """Initialize the hello_c fixture as a git repo if it isn't one already."""
    git_dir = pjoin(FIXTURE_HELLO_C, ".git")
    if not os.path.isdir(git_dir):
        subprocess.check_call(["git", "init", FIXTURE_HELLO_C],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.check_call(
            ["git", "-C", FIXTURE_HELLO_C, "config", "user.email", "ci@example.com"]
        )
        subprocess.check_call(
            ["git", "-C", FIXTURE_HELLO_C, "config", "user.name", "CI"]
        )
        subprocess.check_call(["git", "-C", FIXTURE_HELLO_C, "add", "."],
                              stdout=subprocess.DEVNULL)
        subprocess.check_call(
            ["git", "-C", FIXTURE_HELLO_C, "commit", "-m", "initial commit"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


def test_git_workflow(tmpdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    _ensure_fixture_git_repo()
    mount_process, mnt_dir, src_dir = _mount_sshfs(tmpdir, _no_cache_opts())
    try:
        repo_dir = pjoin(mnt_dir, "repo")
        # Use file:// URL to avoid git's safe-directory checks for local paths
        subprocess.check_call(
            ["git", "clone", "--depth=1", f"file://{FIXTURE_HELLO_C}", repo_dir]
        )
        subprocess.check_call(
            ["git", "-C", repo_dir, "status", "--porcelain"],
        )

        # Configure committer identity in the cloned repo (needed in CI where
        # no global git identity is set)
        subprocess.check_call(
            ["git", "-C", repo_dir, "config", "user.email", "test@example.com"]
        )
        subprocess.check_call(
            ["git", "-C", repo_dir, "config", "user.name", "Test User"]
        )

        new_file = pjoin(repo_dir, "newfile.txt")
        with open(new_file, "w") as fh:
            fh.write("hello from test\n")

        subprocess.check_call(
            ["git", "-C", repo_dir, "add", "newfile.txt"]
        )
        subprocess.check_call(
            [
                "git", "-C", repo_dir, "commit",
                "-m", "test commit",
            ]
        )

        log = subprocess.check_output(
            ["git", "-C", repo_dir, "log", "--oneline"],
            text=True,
        )
        assert "test commit" in log
    except Exception:
        cleanup(mount_process, mnt_dir)
        raise
    else:
        umount(mount_process, mnt_dir)


# ---------------------------------------------------------------------------
# test_rsync_archive
# ---------------------------------------------------------------------------

def test_rsync_archive(tmpdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    # Use the fixture directory as the source (small, reproducible, no build artifacts)
    source = FIXTURE_HELLO_C
    mount_process, mnt_dir, src_dir = _mount_sshfs(tmpdir, _no_cache_opts())
    try:
        dest = pjoin(mnt_dir, "dest")
        os.makedirs(dest)

        subprocess.check_call(
            ["rsync", "-a", "--checksum", source + "/", dest + "/"]
        )

        result = subprocess.run(
            ["diff", "-r", "--no-dereference", source + "/", dest + "/"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"diff found differences after rsync:\n{result.stdout[:2000]}"
        )
    except Exception:
        cleanup(mount_process, mnt_dir)
        raise
    else:
        umount(mount_process, mnt_dir)


# ---------------------------------------------------------------------------
# test_tar_roundtrip
# ---------------------------------------------------------------------------

def test_tar_roundtrip(tmpdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    mount_process, mnt_dir, src_dir = _mount_sshfs(tmpdir, _no_cache_opts())
    try:
        archive = pjoin(mnt_dir, "archive.tar.gz")
        extracted = pjoin(mnt_dir, "extracted")
        os.makedirs(extracted)

        # Use the fixture dir as the source (small, self-contained)
        subprocess.check_call(
            ["tar", "czf", archive, "-C", os.path.dirname(FIXTURE_HELLO_C),
             os.path.basename(FIXTURE_HELLO_C)]
        )

        subprocess.check_call(
            ["tar", "xzf", archive, "-C", extracted]
        )

        result = subprocess.run(
            ["diff", "-r", FIXTURE_HELLO_C,
             pjoin(extracted, os.path.basename(FIXTURE_HELLO_C))],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"tar round-trip diff failed:\n{result.stdout[:2000]}"
        )
    except Exception:
        cleanup(mount_process, mnt_dir)
        raise
    else:
        umount(mount_process, mnt_dir)


# ---------------------------------------------------------------------------
# test_large_file
# ---------------------------------------------------------------------------

def test_large_file(tmpdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    mount_process, mnt_dir, src_dir = _mount_sshfs(tmpdir, _no_cache_opts())
    try:
        SIZE = 64 * 1024 * 1024
        CHUNK = 128 * 1024
        n_chunks = SIZE // CHUNK

        # Generate reproducible random data
        rng_bytes = random.Random(0xdeadbeef)
        chunk = bytes([rng_bytes.getrandbits(8) for _ in range(CHUNK)])
        expected_data = chunk * n_chunks

        path = pjoin(mnt_dir, "largefile")
        with open(path, "wb") as fh:
            for _ in range(n_chunks):
                fh.write(chunk)

        # Sequential read-back with hash verification
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            while True:
                data = fh.read(CHUNK)
                if not data:
                    break
                h.update(data)
        expected_hash = hashlib.sha256(expected_data).digest()
        assert h.digest() == expected_hash, "sequential read-back hash mismatch"

        # Random seek verification at 20 1MB-aligned offsets
        rng = random.Random(42)
        MB = 1024 * 1024
        with open(path, "rb") as fh:
            for _ in range(20):
                offset = rng.randrange(0, SIZE - MB, MB)
                fh.seek(offset)
                got = fh.read(MB)
                assert got == expected_data[offset : offset + MB], (
                    f"random seek read mismatch at offset {offset}"
                )
    except Exception:
        cleanup(mount_process, mnt_dir)
        raise
    else:
        umount(mount_process, mnt_dir)


# ---------------------------------------------------------------------------
# test_find_grep
# ---------------------------------------------------------------------------

def test_find_grep(tmpdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    mount_process, mnt_dir, src_dir = _mount_sshfs(tmpdir, _no_cache_opts())
    try:
        n_dirs = 50
        n_files = 1000

        # Create 1000 files across 50 subdirectories in src_dir
        files_created = []
        for i in range(n_dirs):
            d = pjoin(src_dir, f"dir_{i:03d}")
            os.makedirs(d)
        for i in range(n_files):
            d = pjoin(src_dir, f"dir_{i % n_dirs:03d}")
            fname = f"file_{i:04d}.txt"
            fpath = pjoin(d, fname)
            with open(fpath, "w") as fh:
                # Every 10th file contains the needle
                if i % 10 == 0:
                    fh.write(f"needle in file {i}\n")
                else:
                    fh.write(f"haystack data in file {i}\n")
            files_created.append(pjoin(d.replace(src_dir, mnt_dir), fname))

        # find via mount
        find_mnt = subprocess.check_output(
            ["find", mnt_dir, "-name", "*.txt", "-type", "f"],
            text=True,
        )
        mnt_files = sorted(find_mnt.strip().splitlines())

        # find via src_dir for comparison
        find_src = subprocess.check_output(
            ["find", src_dir, "-name", "*.txt", "-type", "f"],
            text=True,
        )
        src_files = sorted(f.replace(src_dir, mnt_dir) for f in find_src.strip().splitlines())

        assert mnt_files == src_files, "find output via mount differs from src_dir"
        assert len(mnt_files) == n_files, f"expected {n_files} files, found {len(mnt_files)}"

        # grep for needle through mount
        grep_result = subprocess.run(
            ["grep", "-r", "needle", pjoin(mnt_dir)],
            capture_output=True,
            text=True,
        )
        needle_lines = [l for l in grep_result.stdout.strip().splitlines() if l]
        assert len(needle_lines) == n_files // 10, (
            f"expected {n_files // 10} needle matches, got {len(needle_lines)}"
        )
    except Exception:
        cleanup(mount_process, mnt_dir)
        raise
    else:
        umount(mount_process, mnt_dir)


# ---------------------------------------------------------------------------
# test_compile_c_project
# ---------------------------------------------------------------------------

def test_compile_c_project(tmpdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)

    if not shutil.which("gcc") or not shutil.which("make"):
        pytest.skip("gcc and make not available")

    mount_process, mnt_dir, src_dir = _mount_sshfs(tmpdir, _no_cache_opts())
    try:
        project_dir = pjoin(mnt_dir, "hello_c")
        shutil.copytree(FIXTURE_HELLO_C, project_dir,
                        ignore=shutil.ignore_patterns(".git"))

        subprocess.check_call(["make", "-j4", "-C", project_dir])

        binary = pjoin(project_dir, "hello")
        assert os.path.isfile(binary), "compiled binary not found"

        output = subprocess.check_output([binary], text=True)
        assert "Hello, world!" in output, f"unexpected binary output: {output!r}"
    except Exception:
        cleanup(mount_process, mnt_dir)
        raise
    else:
        umount(mount_process, mnt_dir)


# ---------------------------------------------------------------------------
# test_parallel_writers
# ---------------------------------------------------------------------------

def _writer_worker(args):
    path, data = args
    with open(path, "wb") as fh:
        fh.write(data)


def test_parallel_writers(tmpdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    mount_process, mnt_dir, src_dir = _mount_sshfs(
        tmpdir, _no_cache_opts() + ["max_conns=4"]
    )
    try:
        payloads = [
            (pjoin(mnt_dir, f"worker_{i}"), os.urandom(64 * 1024))
            for i in range(8)
        ]

        with multiprocessing.Pool(8) as pool:
            pool.map(_writer_worker, payloads)

        for path, expected in payloads:
            with open(path, "rb") as fh:
                assert fh.read() == expected, f"data mismatch for {path}"
    except Exception:
        cleanup(mount_process, mnt_dir)
        raise
    else:
        umount(mount_process, mnt_dir)


# ---------------------------------------------------------------------------
# test_random_file_ops
# ---------------------------------------------------------------------------

def test_random_file_ops(tmpdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    mount_process, mnt_dir, src_dir = _mount_sshfs(tmpdir, _no_cache_opts())
    try:
        rng = random.Random(0xdeadbeef)
        SIZE = 512 * 1024
        expected = bytearray(SIZE)

        path = pjoin(mnt_dir, "random_test")
        with open(path, "wb") as fh:
            fh.write(b"\x00" * SIZE)

        for _ in range(500):
            op = rng.choice(["write", "truncate", "read_verify"])
            offset = rng.randrange(0, SIZE)

            if op == "write":
                if offset >= len(expected):
                    continue
                length = rng.randint(1, min(8192, len(expected) - offset))
                data = os.urandom(length)
                with open(path, "r+b") as fh:
                    fh.seek(offset)
                    fh.write(data)
                expected[offset : offset + length] = data

            elif op == "truncate":
                new_size = rng.randrange(0, SIZE + 1)
                os.truncate(path, new_size)
                if new_size > len(expected):
                    expected.extend(b"\x00" * (new_size - len(expected)))
                else:
                    del expected[new_size:]

            elif op == "read_verify":
                if offset >= len(expected) or len(expected) == 0:
                    continue
                length = rng.randint(1, min(8192, len(expected) - offset))
                with open(path, "rb") as fh:
                    fh.seek(offset)
                    got = fh.read(length)
                assert got == bytes(expected[offset : offset + length]), (
                    f"read_verify mismatch at offset={offset} length={length}"
                )
    except Exception:
        cleanup(mount_process, mnt_dir)
        raise
    else:
        umount(mount_process, mnt_dir)


# ---------------------------------------------------------------------------
# test_special_filenames
# ---------------------------------------------------------------------------

def test_special_filenames(tmpdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    mount_process, mnt_dir, src_dir = _mount_sshfs(tmpdir, _no_cache_opts())
    try:
        special_names = [
            "file with spaces",
            "file'single_quote",
            'file"double_quote',
            "file\\backslash",
            "file#hash",
            "file%percent",
            "-leading-dash",
        ]

        for name in special_names:
            path = pjoin(mnt_dir, name)
            data = f"content of {name!r}\n".encode()
            with open(path, "wb") as fh:
                fh.write(data)

        listing = os.listdir(mnt_dir)
        for name in special_names:
            assert name in listing, f"{name!r} not found in listdir"
            path = pjoin(mnt_dir, name)
            with open(path, "rb") as fh:
                content = fh.read()
            expected = f"content of {name!r}\n".encode()
            assert content == expected, f"content mismatch for {name!r}"
            fst = os.stat(path)
            assert fst.st_size == len(expected)
            os.unlink(path)
    except Exception:
        cleanup(mount_process, mnt_dir)
        raise
    else:
        umount(mount_process, mnt_dir)


# ---------------------------------------------------------------------------
# test_unicode_filenames
# ---------------------------------------------------------------------------

def test_unicode_filenames(tmpdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    mount_process, mnt_dir, src_dir = _mount_sshfs(tmpdir, _no_cache_opts())
    try:
        unicode_names = [
            "café",          # precomposed e-acute (NFC)
            "café",         # cafe + combining acute (NFD)
            "世界",       # CJK: 世界
            "\U0001f4c1folder",   # emoji
        ]

        for name in unicode_names:
            path = pjoin(mnt_dir, name)
            data = f"unicode {name!r}\n".encode("utf-8")
            with open(path, "wb") as fh:
                fh.write(data)

        listing = os.listdir(mnt_dir)
        for name in unicode_names:
            assert name in listing, f"{name!r} not in listdir"
            path = pjoin(mnt_dir, name)
            with open(path, "rb") as fh:
                content = fh.read()
            expected = f"unicode {name!r}\n".encode("utf-8")
            assert content == expected, f"content mismatch for {name!r}"
            os.unlink(path)
    except Exception:
        cleanup(mount_process, mnt_dir)
        raise
    else:
        umount(mount_process, mnt_dir)


# ---------------------------------------------------------------------------
# test_sparse_file
# ---------------------------------------------------------------------------

def test_sparse_file(tmpdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    mount_process, mnt_dir, src_dir = _mount_sshfs(tmpdir, _no_cache_opts())
    try:
        path = pjoin(mnt_dir, "sparse")

        BLOCK = 4096
        HOLE_START = BLOCK
        HOLE_END = 1024 * 1024
        SECOND_WRITE = HOLE_END

        # Write at offset 0
        with open(path, "wb") as fh:
            fh.write(b"A" * BLOCK)

        # Write at offset 1MB (skip ~1MB - 4KB of hole)
        with open(path, "r+b") as fh:
            fh.seek(SECOND_WRITE)
            fh.write(b"B" * BLOCK)

        expected_size = SECOND_WRITE + BLOCK
        fst = os.stat(path)
        assert fst.st_size == expected_size, (
            f"expected size {expected_size}, got {fst.st_size}"
        )

        # Read back and verify hole is zeros
        with open(path, "rb") as fh:
            first_block = fh.read(BLOCK)
            assert first_block == b"A" * BLOCK

            hole = fh.read(HOLE_END - HOLE_START)
            assert hole == b"\x00" * (HOLE_END - HOLE_START), (
                "hole region should be all zeros"
            )

            second_block = fh.read(BLOCK)
            assert second_block == b"B" * BLOCK

        # Truncate-extend past end and verify new region is zeros
        extended_size = expected_size + 1024 * 1024
        os.truncate(path, extended_size)
        fst = os.stat(path)
        assert fst.st_size == extended_size

        with open(path, "rb") as fh:
            fh.seek(expected_size)
            tail = fh.read(1024 * 1024)
        assert tail == b"\x00" * (1024 * 1024), "region past old end should be zeros"

        os.unlink(path)
    except Exception:
        cleanup(mount_process, mnt_dir)
        raise
    else:
        umount(mount_process, mnt_dir)
