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
import signal
import stat
import subprocess
import time
import pytest
from os.path import join as pjoin
from util import fuse_test_marker, mounted, require_fuse

pytestmark = fuse_test_marker()


def require(tool):
    if shutil.which(tool):
        return
    # a missing tool must not turn into a skip on CI
    if require_fuse():
        pytest.fail(f"{tool} is not installed")
    pytest.skip(f"{tool} is not installed")


def run(cmd, **kwargs):
    """Run *cmd* and return its output. Fail with the output if it fails.

    The command gets its own process group, which is killed if it times out
    or the test is interrupted, so that nothing is left using the mount.
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        start_new_session=True,
        **kwargs,
    )
    try:
        out, _ = proc.communicate(timeout=120)
    except BaseException:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass  # stuck in the mount, which mounted() then aborts
        raise
    assert (
        proc.returncode == 0
    ), f"{' '.join(cmd)} exited with {proc.returncode}:\n{out}"
    return out


def raise_error(exc):
    """onerror for os.walk(), which would ignore a failing scandir() otherwise"""
    raise exc


def wait_for_hidden_files(root, timeout=10):
    """Wait until libfuse has removed the .fuse_hidden* files below *root*

    A file that is unlinked while libfuse still counts it as open is renamed
    instead, and only removed once the (asynchronous) release arrives.
    """

    def hidden():
        return [
            pjoin(d, n)
            for d, _, names in os.walk(root, onerror=raise_error)
            for n in names
            if n.startswith(".fuse_hidden")
        ]

    deadline = time.monotonic() + timeout
    while hidden():
        # AssertionError, because this also runs in pool workers
        if time.monotonic() > deadline:
            raise AssertionError(
                f"hidden files still there after {timeout}s: {hidden()}"
            )
        time.sleep(0.05)


def write_tree(root, files):
    """Create the files (relative path -> bytes) below *root*"""
    for rel, data in files.items():
        path = pjoin(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)


def check_tree(root, files):
    """The files below *root* are exactly *files* (relative path -> bytes)"""
    found = {}
    for dirpath, _, names in os.walk(root, onerror=raise_error):
        for name in names:
            path = pjoin(dirpath, name)
            with open(path, "rb") as fh:
                found[os.path.relpath(path, root)] = fh.read()
    assert sorted(found) == sorted(files)
    for rel, data in files.items():
        assert found[rel] == data, rel


def test_rsync_archive(tmpdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    require("rsync")

    files = {
        "file_a.txt": b"alpha\n",
        "file_b.txt": b"beta\n",
        "data.bin": bytes(range(256)) * 64,
        "sub/file_c.txt": b"gamma\n",
        "sub/deeper/file_d.txt": b"delta\n",
    }
    source = str(tmpdir.mkdir("rsync_source"))
    write_tree(source, files)
    os.chmod(pjoin(source, "file_b.txt"), 0o755)
    # No symlink in the tree on purpose. SFTP can't set the times of a link
    # itself, so rsync's utimensat() on a link whose target isn't there yet
    # fails with ENOENT, depending on how rsync's processes interleave.

    with mounted(tmpdir) as (mnt_dir, src_dir):
        dest = pjoin(mnt_dir, "rsync_dest")
        run(["rsync", "-a", source + "/", dest + "/"])
        check_tree(dest, files)
        assert stat.S_IMODE(os.stat(pjoin(dest, "file_b.txt")).st_mode) == 0o755


def test_tar_roundtrip(tmpdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    require("tar")

    files = {
        "readme.txt": b"tar round-trip test\n",
        "data.bin": b"\x00\x01\x02\x03" * 256,
        "subdir/nested.txt": b"nested file content\n",
    }
    with mounted(tmpdir) as (mnt_dir, src_dir):
        # Build the tree through the mount so the write path goes via sshfs too
        write_tree(pjoin(mnt_dir, "tar_source"), files)

        archive = pjoin(mnt_dir, "archive.tar.gz")
        run(["tar", "czf", archive, "-C", mnt_dir, "tar_source"])

        extract_dir = pjoin(mnt_dir, "extracted")
        os.mkdir(extract_dir)
        run(["tar", "xzf", archive, "-C", extract_dir])

        check_tree(pjoin(extract_dir, "tar_source"), files)


def test_cp_archive(tmpdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    require("cp")

    files = {
        "file_a.txt": b"alpha\n",
        "file_b.txt": b"beta\n",
        "subdir/file_c.txt": b"gamma\n",
    }
    with mounted(tmpdir) as (mnt_dir, src_dir):
        tree = pjoin(mnt_dir, "cp_source")
        write_tree(tree, files)
        dest = pjoin(mnt_dir, "cp_dest")
        run(["cp", "-a", tree, dest])
        check_tree(dest, files)


def test_find_grep(tmpdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    require("find")
    require("grep")

    num_dirs = 50
    num_files = 1000
    needle_interval = 10  # every 10th file contains "needle"

    with mounted(tmpdir) as (mnt_dir, src_dir):
        # Create the tree on the server side, it's faster than going through
        # the mount
        expected = set()
        needles = set()
        for d in range(num_dirs):
            os.makedirs(pjoin(src_dir, f"dir_{d:03d}"))
        for i in range(num_files):
            rel = pjoin(f"dir_{i % num_dirs:03d}", f"file_{i:04d}.txt")
            word = "needle" if i % needle_interval == 0 else "haystack"
            with open(pjoin(src_dir, rel), "w") as fh:
                fh.write(f"{word} in file {i}\n")
            expected.add(pjoin(mnt_dir, rel))
            if word == "needle":
                needles.add(f"{pjoin(mnt_dir, rel)}:needle in file {i}")

        found = run(["find", mnt_dir, "-name", "*.txt", "-type", "f"]).splitlines()
        assert len(found) == num_files
        assert set(found) == expected

        out = run(["grep", "-r", "needle", mnt_dir])
        assert len(needles) == num_files // needle_interval
        assert sorted(out.splitlines()) == sorted(needles)


HELLO_C = {
    "main.c": '#include <stdio.h>\n#include "util.h"\n\nint main(void) {\n    greet();\n    return 0;\n}\n',
    "util.c": '#include <stdio.h>\n#include "util.h"\n\nvoid greet(void) {\n    printf("Hello, world!\\n");\n}\n',
    "util.h": "#ifndef UTIL_H\n#define UTIL_H\nvoid greet(void);\n#endif\n",
    "Makefile": (
        "CFLAGS ?= -Wall -Wextra\n\n"
        "hello: main.o util.o\n\t$(CC) $(CFLAGS) -o $@ $^\n\n"
        "%.o: %.c util.h\n\t$(CC) $(CFLAGS) -c -o $@ $<\n\n"
        "clean:\n\trm -f *.o hello\n\n"
        ".PHONY: clean\n"
    ),
}


def test_compile_c_project(tmpdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    require("make")
    require("cc")

    with mounted(tmpdir) as (mnt_dir, src_dir):
        project = pjoin(mnt_dir, "hello_c")
        write_tree(project, {k: v.encode() for k, v in HELLO_C.items()})

        run(["make", "-j4", "-C", project])
        assert os.path.isfile(pjoin(project, "hello"))
        assert run([pjoin(project, "hello")]) == "Hello, world!\n"

        run(["make", "-C", project, "clean"])
        for name in ("hello", "main.o", "util.o"):
            assert not os.path.exists(pjoin(project, name))


def test_git_workflow(tmpdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    require("git")

    # Don't pick up the identity or settings of whoever runs the tests
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_ATTR_NOSYSTEM="1",
        GIT_AUTHOR_NAME="Test",
        GIT_AUTHOR_EMAIL="test@example.com",
        GIT_COMMITTER_NAME="Test",
        GIT_COMMITTER_EMAIL="test@example.com",
    )

    # no user-level attributes or ignore rules either
    git_cmd = [
        "git",
        "-c",
        "core.attributesFile=/dev/null",
        "-c",
        "core.excludesFile=/dev/null",
    ]

    def git(repo, *args):
        return run(git_cmd + ["-C", repo] + list(args), env=env)

    origin = str(tmpdir.mkdir("origin"))
    write_tree(origin, {k: v.encode() for k, v in HELLO_C.items()})
    run(git_cmd + ["-c", "init.defaultBranch=main", "init", origin], env=env)
    git(origin, "add", ".")
    git(origin, "commit", "-m", "initial")

    with mounted(tmpdir) as (mnt_dir, src_dir):
        # file:// forces a real transfer instead of hardlinking objects
        repo = pjoin(mnt_dir, "repo")
        run(git_cmd + ["clone", f"file://{origin}", repo], env=env)
        assert git(repo, "status", "--porcelain") == ""

        newfile = pjoin(repo, "newfile.txt")
        with open(newfile, "w") as fh:
            fh.write("hello from test\n")
        git(repo, "add", "newfile.txt")
        git(repo, "commit", "-m", "add newfile")
        assert "add newfile" in git(repo, "log", "--oneline")
        with open(newfile, "rb") as fh:
            assert fh.read() == b"hello from test\n"

        with open(newfile, "w") as fh:
            fh.write("modified content\n")
        assert "modified content" in git(repo, "diff")
        git(repo, "checkout", "--", "newfile.txt")
        with open(newfile, "rb") as fh:
            assert fh.read() == b"hello from test\n"

        git(repo, "gc", "--quiet")
        git(repo, "fsck", "--strict")


# Needs to be at module level so that it can be pickled for the pool
def _stress_worker(args):
    """Random file system operations in a private tree, checked against a model

    Every worker also creates and removes files in one shared directory, so
    that there is contention on a directory and on the connections. Returns
    the digests of the files the worker left in the shared directory.
    """
    idx, mnt_dir, nops = args
    rng = random.Random(1000 + idx)
    root = pjoin(mnt_dir, f"w{idx}")
    shared = pjoin(mnt_dir, "shared")
    os.mkdir(root)

    files = {}  # path relative to root -> contents
    dirs = {""}  # relative paths of directories, "" is the root
    shared_files = {}  # name -> contents
    counter = 0

    def path(rel):
        return pjoin(root, rel) if rel else root

    def child(parent, name):
        return pjoin(parent, name) if parent else name

    def new_name():
        nonlocal counter
        counter += 1
        return f"n{counter}"

    def pick(items):
        return rng.choice(sorted(items))

    def empty_dirs():
        return [
            d
            for d in dirs
            if d
            and not any(f.startswith(d + "/") for f in files)
            and not any(o.startswith(d + "/") for o in dirs)
        ]

    for _ in range(nops):
        op = rng.choice(
            [
                "create", "create", "append", "overwrite", "truncate", "read",
                "stat", "rename", "unlink", "mkdir", "rmdir", "shared_create",
                "shared_unlink",
            ]
        )  # fmt: skip

        if op == "create":
            rel = child(pick(dirs), new_name())
            data = rng.randbytes(rng.randint(0, 20000))
            with open(path(rel), "wb") as fh:
                fh.write(data)
            files[rel] = data

        elif op == "mkdir":
            rel = child(pick(dirs), new_name())
            os.mkdir(path(rel))
            dirs.add(rel)

        elif op == "rmdir":
            candidates = empty_dirs()
            if candidates:
                rel = pick(candidates)
                wait_for_hidden_files(path(rel))
                os.rmdir(path(rel))
                dirs.remove(rel)

        elif op == "shared_create":
            name = f"w{idx}_{new_name()}"
            data = rng.randbytes(rng.randint(1, 5000))
            with open(pjoin(shared, name), "wb") as fh:
                fh.write(data)
            shared_files[name] = data

        elif op == "shared_unlink":
            if shared_files:
                name = pick(shared_files)
                os.unlink(pjoin(shared, name))
                del shared_files[name]

        elif files:
            rel = pick(files)
            data = files[rel]

            if op == "append":
                extra = rng.randbytes(rng.randint(1, 5000))
                with open(path(rel), "ab") as fh:
                    fh.write(extra)
                files[rel] = data + extra

            elif op == "overwrite":
                offset = rng.randint(0, len(data) + 100)
                extra = rng.randbytes(rng.randint(1, 5000))
                fd = os.open(path(rel), os.O_WRONLY)
                try:
                    os.pwrite(fd, extra, offset)
                finally:
                    os.close(fd)
                padded = data + b"\0" * max(0, offset - len(data))
                files[rel] = padded[:offset] + extra + padded[offset + len(extra) :]

            elif op == "truncate":
                size = rng.randint(0, 25000)
                os.truncate(path(rel), size)
                files[rel] = (data + b"\0" * size)[:size]

            elif op == "read":
                with open(path(rel), "rb") as fh:
                    assert fh.read() == data, f"worker {idx}: wrong contents in {rel}"

            elif op == "stat":
                assert os.stat(path(rel)).st_size == len(data)

            elif op == "rename":
                new_rel = child(pick(dirs), new_name())
                os.rename(path(rel), path(new_rel))
                files[new_rel] = files.pop(rel)

            elif op == "unlink":
                os.unlink(path(rel))
                del files[rel]

    # The tree on disk has to match the model exactly
    wait_for_hidden_files(root)
    found_files, found_dirs = {}, {""}
    for dirpath, dirnames, filenames in os.walk(root, onerror=raise_error):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir
        for name in dirnames:
            found_dirs.add(child(rel_dir, name))
        for name in filenames:
            with open(pjoin(dirpath, name), "rb") as fh:
                found_files[child(rel_dir, name)] = fh.read()
    assert found_dirs == dirs, f"worker {idx}: directories differ"
    assert found_files == files, f"worker {idx}: files differ"

    for name, data in shared_files.items():
        with open(pjoin(shared, name), "rb") as fh:
            assert fh.read() == data, f"worker {idx}: wrong contents in shared/{name}"
    return {
        name: hashlib.sha256(data).hexdigest() for name, data in shared_files.items()
    }


@pytest.mark.parametrize("max_conns", [1, 4])
def test_concurrent_stress(tmpdir, capfd, max_conns):
    """Parallel processes doing random file system operations

    A small stand-in for fsstress that needs no extra packages.
    """
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    workers = 4
    nops = 600

    ctx = multiprocessing.get_context("fork")
    # The pool goes outside mounted(): if a worker hangs in the mount, the
    # timeout unwinds through mounted() and aborts the mount before the
    # pool tries to stop the workers.
    with ctx.Pool(workers) as pool:
        with mounted(tmpdir, [f"max_conns={max_conns}"]) as (mnt_dir, src_dir):
            os.mkdir(pjoin(mnt_dir, "shared"))
            results = pool.map_async(
                _stress_worker, [(i, mnt_dir, nops) for i in range(workers)]
            ).get(timeout=120)

            # Nothing got lost or leaked into the shared directory, as seen
            # from the server
            expected = {}
            for digests in results:
                expected.update(digests)
            on_server = pjoin(src_dir, "shared")
            wait_for_hidden_files(on_server)
            assert sorted(os.listdir(on_server)) == sorted(expected)
            for name, digest in expected.items():
                with open(pjoin(on_server, name), "rb") as fh:
                    assert hashlib.sha256(fh.read()).hexdigest() == digest

            # And the mount is still usable
            canary = pjoin(mnt_dir, "canary")
            with open(canary, "wb") as fh:
                fh.write(b"still alive")
            with open(canary, "rb") as fh:
                assert fh.read() == b"still alive"
