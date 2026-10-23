#!/usr/bin/env python3
"""Protocol edge cases, with sshfs mounted on a scripted SFTP server.

mock_sftp_server.py stands in for ssh (-o ssh_command), so these need FUSE but
not sshd, and the server can do what OpenSSH's never does.

An xfail(strict=True, raises=KnownBug) test documents an sshfs bug. It raises
KnownBug only when the documented symptom shows up; any other failure is a
failure. Drop the marker when the fix lands: strict makes CI remind us.
"""

if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main([__file__] + sys.argv[1:]))

import ctypes
import errno
import glob
import grp
import json
import os
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from os.path import join as pjoin

import pytest

from util import base_cmdline, basename, cleanup, fuse_test_marker

pytestmark = fuse_test_marker()

MOCK = pjoin(os.path.dirname(os.path.abspath(__file__)), "mock_sftp_server.py")

# The words conftest.py rejects in output, plus sanitizer names. conftest only
# checks a test that returns, so an xfail would get past it.
SUSPICIOUS = re.compile(
    r"\b(?:exception|error|warning|fatal|traceback|fault|crash(?:ed)?|abort(?:ed)|"
    r"uninitiali[zs]ed)\b|Sanitizer|^==[0-9]+==",
    re.IGNORECASE | re.MULTILINE,
)

FUSERMOUNT = shutil.which("fusermount3") or "fusermount"


def sanitizer_log_files():
    """Report files the sanitizers write when the CI job sets log_path."""
    files = set()
    for var in ("ASAN_OPTIONS", "LSAN_OPTIONS", "UBSAN_OPTIONS", "TSAN_OPTIONS"):
        for opt in os.environ.get(var, "").split(":"):
            if opt.startswith("log_path="):
                files.update(glob.glob(opt[len("log_path="):] + ".*"))
    return files


class KnownBug(Exception):
    """The documented symptom of a known sshfs bug was observed."""


class MountFailed(Exception):
    """sshfs exited, or never mounted, before the test could start."""

    def __init__(self, message, stderr=""):
        super().__init__(f"{message}\nsshfs stderr:\n{stderr}")
        self.stderr = stderr


class Mount:
    def __init__(self, tmpdir, proc):
        self.src = pjoin(str(tmpdir), "src")
        self.mnt = pjoin(str(tmpdir), "mnt")
        self.proc = proc
        self.mounted = False
        self.state_path = pjoin(str(tmpdir), "mock-state")
        self.stats_path = pjoin(str(tmpdir), "mock-stats.json")
        self.stderr_path = pjoin(str(tmpdir), "sshfs.stderr")

    def stats(self):
        """Counters kept by the mock (see its docstring)."""
        try:
            with open(self.stats_path) as fh:
                return json.load(fh)
        except (FileNotFoundError, ValueError):
            return {}

    def connections(self):
        """How many times the mock has been started."""
        with open(self.state_path) as fh:
            return int(fh.read())

    def stderr_text(self):
        with open(self.stderr_path, "rb") as fh:
            return fh.read().decode(errors="replace")


def wait_for_mount(m, timeout=30):
    """Like util.wait_for_mount, but failing with MountFailed.

    With -f the mount shows up before sshfs has connected, and ismount()
    blocks until the first request is answered or sshfs gives up.
    """
    deadline = time.monotonic() + timeout
    while not os.path.ismount(m.mnt):
        if m.proc.poll() is not None:
            break
        if time.monotonic() > deadline:
            raise MountFailed("mountpoint failed to come up", m.stderr_text())
        time.sleep(0.05)
    if m.proc.poll() is not None:
        raise MountFailed(f"sshfs exited with {m.proc.returncode}", m.stderr_text())
    m.mounted = True


def stop(proc, mnt):
    """Unmount and let sshfs exit on its own, so that its exit-time checks
    (LeakSanitizer) run. Signals are the fallback; returns True if they were
    needed."""
    subprocess.call([FUSERMOUNT, "-z", "-u", mnt], stdout=subprocess.DEVNULL,
                    stderr=subprocess.STDOUT)
    with suppress(subprocess.TimeoutExpired):
        proc.wait(10)
    if proc.poll() is None:
        cleanup(proc, mnt)
        proc.wait(5)
        return True
    return False


@contextmanager
def mock_mount(tmpdir, config=None, opts=(), remote=None, stderr_ok=(), exit_code=None):
    """Mount <tmpdir>/src through the mock server and yield a Mount.

    sshfs must stay up until the test ends, or if exit_code is given, exit
    with that status; stderr_ok lists stderr lines that are expected. Even
    when the body raised an expected failure, these fail the test: sshfs
    exiting early or being killed, a signal, a sanitizer report, and any
    stderr line conftest would reject (the mock shares that stderr).
    """
    base = str(tmpdir)
    src = pjoin(base, "src")
    mnt = pjoin(base, "mnt")
    os.makedirs(src, exist_ok=True)
    os.makedirs(mnt, exist_ok=True)

    # sshfs and FUSE split ssh_command on spaces and commas, so use a bare name:
    # a wrapper script found through PATH runs the mock by its full path
    wrapper = pjoin(base, "bin", "sshfs-mock-sftp")
    os.makedirs(os.path.dirname(wrapper))
    with open(wrapper, "w") as fh:
        fh.write(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -I {shlex.quote(MOCK)} "$@"\n')
    os.chmod(wrapper, 0o755)

    cmdline = base_cmdline + [
        pjoin(basename, "sshfs"),
        "-f",
        f"mock:{remote or src}",
        mnt,
        "-o", "ssh_command=sshfs-mock-sftp",
        "-o", "entry_timeout=0",
        "-o", "attr_timeout=0",
    ]
    for opt in opts:
        cmdline += ["-o", opt]

    env = dict(os.environ)
    env["PATH"] = os.path.dirname(wrapper) + os.pathsep + env["PATH"]
    env["G_DEBUG"] = "fatal-warnings"
    env["SSHFS_MOCK_ROOT"] = base
    env["SSHFS_MOCK_STATE"] = pjoin(base, "mock-state")
    env["SSHFS_MOCK_CONFIG"] = json.dumps(dict(config or {}, stats=pjoin(base, "mock-stats.json")))

    reports_before = sanitizer_log_files()
    with open(pjoin(base, "sshfs.stderr"), "wb") as err:
        proc = subprocess.Popen(cmdline, env=env, stderr=err)
    m = Mount(tmpdir, proc)
    problems = []
    try:
        wait_for_mount(m)
        yield m
    finally:
        early = proc.poll()  # not None: sshfs went away by itself
        killed = stop(proc, mnt)
        if killed:
            problems.append("sshfs had to be killed")
        if m.mounted and exit_code is None:
            if early is not None:
                problems.append(f"sshfs exited during the test, with status {early}")
            elif not killed and proc.returncode != 0:
                problems.append(f"sshfs exited with status {proc.returncode} after the unmount")
        elif m.mounted and proc.returncode != exit_code:
            problems.append(f"sshfs exited with status {proc.returncode}, not {exit_code}")
        text = m.stderr_text()
        if sys.exc_info()[0] is not None:
            sys.stderr.write(text)
        if proc.returncode is not None and proc.returncode < 0:
            problems.append(f"sshfs died of signal {-proc.returncode}")
        for path in sorted(sanitizer_log_files() - reports_before):
            with open(path, errors="replace") as fh:
                problems.append(f"sanitizer report {path}:\n{fh.read()}")
        bad = [l for l in text.splitlines() if l.strip() not in stderr_ok and SUSPICIOUS.search(l)]
        if bad:
            problems.append("unexpected output on sshfs stderr:\n" + "\n".join(bad))
        if problems:
            pytest.fail("\n".join(problems) + f"\nsshfs stderr:\n{text}")


def write_file(path, data):
    with open(path, "wb") as fh:
        fh.write(data)


def read_file(path):
    with open(path, "rb") as fh:
        return fh.read()


def read_all(path):
    """What a reader that loops until EOF gets."""
    chunks = []
    fd = os.open(path, os.O_RDONLY)
    try:
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(fd)


def test_smoke(tmpdir):
    """The harness itself: mount, list, read, write, and the other calls."""
    data = os.urandom(100000)
    with mock_mount(tmpdir) as m:
        write_file(pjoin(m.src, "a"), data)
        assert os.listdir(m.mnt) == ["a"]
        assert read_file(pjoin(m.mnt, "a")) == data
        write_file(pjoin(m.mnt, "b"), data[:5000])
        assert read_file(pjoin(m.src, "b")) == data[:5000]
        os.rename(pjoin(m.mnt, "b"), pjoin(m.mnt, "c"))
        os.unlink(pjoin(m.mnt, "c"))
        assert os.listdir(m.src) == ["a"]

        os.mkdir(pjoin(m.mnt, "d"))
        assert os.path.isdir(pjoin(m.src, "d"))
        os.rmdir(pjoin(m.mnt, "d"))
        os.symlink("a", pjoin(m.mnt, "l"))
        assert os.readlink(pjoin(m.src, "l")) == "a"
        assert os.readlink(pjoin(m.mnt, "l")) == "a"
        os.link(pjoin(m.mnt, "a"), pjoin(m.mnt, "h"))
        assert os.stat(pjoin(m.src, "a")).st_nlink == 2
        os.chmod(pjoin(m.mnt, "a"), 0o600)
        assert os.stat(pjoin(m.src, "a")).st_mode & 0o777 == 0o600
        os.truncate(pjoin(m.mnt, "a"), 10)
        assert os.stat(pjoin(m.src, "a")).st_size == 10
        assert os.statvfs(m.mnt).f_namemax > 0
        fd = os.open(pjoin(m.mnt, "h"), os.O_WRONLY)
        os.write(fd, b"x")
        os.fsync(fd)
        os.close(fd)
        # the extensions the server advertises are the ones in use
        stats = m.stats()
        for ext in ("posix-rename", "statvfs", "hardlink", "fsync"):
            assert stats.get(f"ext_{ext}@openssh.com", 0) > 0, ext


@pytest.mark.parametrize(
    "read_max, direct_io",
    [
        pytest.param(
            1000,
            False,
            marks=pytest.mark.xfail(
                strict=True,
                raises=KnownBug,
                reason="upstream issue 322: a short DATA reply is taken for EOF",
            ),
            id="page_cache",
        ),
        pytest.param(1000, True, id="direct_io-1000"),
        pytest.param(16384, True, id="direct_io-16384"),
    ],
)
def test_short_data_replies(tmpdir, read_max, direct_io):
    """A server may answer a READ with less data than asked for; that is not
    EOF. Check what a reader that loops until EOF gets, not syscall lengths.
    The page cache takes a short reply for the end of the file, while with
    direct_io the reader just sees a short read().
    """
    data = os.urandom(300000)
    opts = ["direct_io"] if direct_io else []
    with mock_mount(tmpdir, {"read_max": read_max}, opts) as m:
        write_file(pjoin(m.src, "f"), data)
        got = read_all(pjoin(m.mnt, "f"))
        assert m.stats().get("short_replies", 0) > 0, "mock never shortened a reply"
        if got != data:
            assert 0 < len(got) < len(data) and data.startswith(got), "wrong data, not a short file"
            raise KnownBug(f"reader got {len(got)} of {len(data)} bytes")


# fault, offset, errno sshfs maps it to: STATUS 3 is permission denied, the
# others are protocol violations that sshfs_read_end() turns into EIO
READ_FAULTS = [
    pytest.param({"status": 3}, 0, errno.EACCES, id="status-at-0"),
    pytest.param({"status": 3}, 65536, errno.EACCES, id="status-at-64k"),
    pytest.param({"reply": "wrong_type"}, 0, errno.EIO, id="wrong-reply-type"),
    pytest.param({"reply": "long"}, 0, errno.EIO, id="data-longer-than-asked"),
]


@pytest.mark.xfail(
    strict=True,
    raises=KnownBug,
    reason="upstream issues 224 and 83: wait_chunk() turns a failed READ into EOF, or a zeroed page on older kernels (fix: upstream PR 240)",
)
@pytest.mark.parametrize("fault, offset, expected", READ_FAULTS)
def test_read_failure_reaches_application(tmpdir, fault, offset, expected):
    """A READ that fails must fail the read(), not look like a short file.

    The agreed fix fails the whole FUSE request, so only the errno is checked,
    not how many bytes came back first.
    """
    config = {"read_error": dict(fault, name="f", offset=offset)}
    with mock_mount(tmpdir, config, stderr_ok=("protocol error", "long read")) as m:
        data = os.urandom(200000)
        write_file(pjoin(m.src, "f"), data)
        error = None
        try:
            got = read_all(pjoin(m.mnt, "f"))
        except OSError as exc:
            error = exc
        assert m.stats().get("injected", 0) > 0, "mock never sent the bad reply"
        if error is None:
            # all the data before the fault, then nothing or (ubuntu 22.04) a zeroed page
            tail = got[offset:]
            assert len(got) >= offset and got[:offset] == data[:offset], "data before the fault is missing or wrong"
            assert not tail or (len(tail) <= os.sysconf("SC_PAGESIZE") and not any(tail)), "data after the fault"
            raise KnownBug(f"no error; {len(got)} of {len(data)} bytes")
        assert error.errno == expected, error


def test_write_error_reported(tmpdir):
    """A failed asynchronous WRITE must surface by close() at the latest."""
    with mock_mount(tmpdir, {"fail": {"write": 4}}) as m:
        errors = []
        fh = open(pjoin(m.mnt, "f"), "wb", buffering=0)
        for step in (lambda: fh.write(os.urandom(100000)), fh.close):
            try:
                step()
            except OSError as exc:
                errors.append(exc)
        assert m.stats().get("injected", 0) > 0, "mock never failed a WRITE"
        assert errors, "write error was dropped"
        assert all(e.errno == errno.EIO for e in errors), errors


@pytest.mark.xfail(
    strict=True,
    raises=KnownBug,
    reason="sshfs_write_end() ignores a malformed WRITE reply (fix pending in upstream PR 391)",
)
@pytest.mark.parametrize("how", ["close", "fsync", "sshfs_sync"])
@pytest.mark.parametrize("reply", ["wrong_type", "truncated"])
def test_malformed_write_reply(tmpdir, reply, how):
    """A reply to WRITE that is not a complete STATUS is an I/O error.

    Writes are asynchronous, so the error comes out of fsync() or close();
    with -o sshfs_sync, out of write() itself.
    """
    opts = ["sshfs_sync"] if how == "sshfs_sync" else []
    with mock_mount(tmpdir, {"write_reply": reply}, opts, stderr_ok=("protocol error",)) as m:
        fd = os.open(pjoin(m.mnt, "f"), os.O_WRONLY | os.O_CREAT, 0o644)
        errors = []
        steps = [lambda: os.write(fd, os.urandom(50000))]
        if how == "fsync":
            steps.append(lambda: os.fsync(fd))
        steps.append(lambda: os.close(fd))
        for step in steps:
            try:
                step()
            except OSError as exc:
                errors.append(exc)
        assert m.stats().get("injected", 0) > 0, "mock never sent the bad reply"
        if not errors:
            raise KnownBug("write, fsync and close all succeeded")
        assert all(e.errno == errno.EIO for e in errors), errors


@pytest.mark.parametrize(
    "status, expected",
    [
        pytest.param(2, errno.ENOENT, id="no_such_file"),
        pytest.param(3, errno.EACCES, id="permission_denied"),
        pytest.param(4, errno.EPERM, id="failure"),
        pytest.param(5, errno.EBADMSG, id="bad_message"),
        pytest.param(6, errno.ENOTCONN, id="no_connection"),
        pytest.param(7, errno.ECONNABORTED, id="connection_lost"),
        pytest.param(8, errno.EOPNOTSUPP, id="op_unsupported"),
        pytest.param(99, errno.EIO, id="unknown"),
    ],
)
def test_error_status_codes(tmpdir, status, expected):
    """STATUS codes OpenSSH never sends (6, 7, unknown ones) still map to errno."""
    with mock_mount(tmpdir, {"fail": {"remove": status}}) as m:
        write_file(pjoin(m.src, "f"), b"x")
        with pytest.raises(OSError) as exc_info:
            os.unlink(pjoin(m.mnt, "f"))
        assert exc_info.value.errno == expected
        assert os.path.exists(pjoin(m.src, "f"))


@pytest.mark.xfail(
    strict=True,
    raises=KnownBug,
    reason="sshfs_mkdir() turns every FAILURE reply into EEXIST: access(R_OK) always succeeds",
)
def test_mkdir_failure_is_not_eexist(tmpdir):
    """OpenSSH answers MKDIR with FAILURE for ENOSPC, EROFS and others too.

    sshfs says EEXIST whenever it gets FAILURE, even for a name that is not
    there. EPERM or EIO would be right.
    """
    with mock_mount(tmpdir, {"fail": {"mkdir": 4}}) as m:
        with pytest.raises(OSError) as exc_info:
            os.mkdir(pjoin(m.mnt, "d"))
        assert m.stats().get("injected", 0) > 0, "mock never failed the MKDIR"
        assert not os.path.exists(pjoin(m.src, "d"))
        if exc_info.value.errno == errno.EEXIST:
            raise KnownBug("mkdir of a missing name failed with EEXIST")
        assert exc_info.value.errno in (errno.EPERM, errno.EIO), exc_info.value


@pytest.mark.xfail(
    strict=True,
    raises=KnownBug,
    reason="sshfs_chown() runs the (uid_t)-1 'unchanged' id through translate_id()",
)
@pytest.mark.parametrize("which", ["chgrp", "chown"])
def test_chown_one_id_with_idmap_file(tmpdir, which):
    """chgrp and chown with -1 for the other id, both ids in the map.

    nomap=error is the default and -1 is in no map, so sshfs says EPERM.
    """
    uid, gid = os.getuid(), os.getgid()
    groups = {}
    for g in [gid] + os.getgroups():
        with suppress(KeyError):
            groups[g] = grp.getgrgid(g).gr_name
    assert gid in groups, "idmap=file needs a name for the test group"
    # a second group, so that chgrp changes something
    other = next((g for g in groups if g != gid), gid)
    args, want = ((-1, other), (uid, other)) if which == "chgrp" else ((uid, -1), (uid, gid))

    tmpdir.join("uidfile").write(f"{pwd.getpwuid(uid).pw_name}:{uid}\n")
    tmpdir.join("gidfile").write("".join(f"{groups[g]}:{g}\n" for g in {gid, other}))
    opts = ["idmap=file", f"uidfile={tmpdir.join('uidfile')}", f"gidfile={tmpdir.join('gidfile')}"]
    with mock_mount(tmpdir, opts=opts) as m:
        write_file(pjoin(m.src, "f"), b"x")
        try:
            os.chown(pjoin(m.mnt, "f"), *args)
        except PermissionError as exc:
            # sshfs refuses with EPERM itself; the server would say EACCES
            assert exc.errno == errno.EPERM, exc
            assert not m.stats().get("setstat"), "the request reached the server"
            raise KnownBug("chown with an unchanged id failed with EPERM, locally")
        assert m.stats().get("setstat", 0) > 0, "no SETSTAT reached the server"
        st = os.stat(pjoin(m.src, "f"))
        assert (st.st_uid, st.st_gid) == want


@pytest.mark.xfail(strict=True, raises=MountFailed, reason="upstream issue 312: sftp_check_root() uses LSTAT")
def test_symlinked_root(tmpdir):
    """Mounting a remote path that is a symlink to a directory."""
    os.makedirs(pjoin(str(tmpdir), "real"))
    write_file(pjoin(str(tmpdir), "real", "x"), b"x")
    os.symlink("real", pjoin(str(tmpdir), "link"))
    try:
        with mock_mount(tmpdir, remote=pjoin(str(tmpdir), "link")) as m:
            assert os.listdir(m.mnt) == ["x"]
    except MountFailed as exc:
        assert "Not a directory" in exc.stderr, exc
        raise


LINUX_LP64 = sys.platform.startswith("linux") and ctypes.sizeof(ctypes.c_void_p) == 8


class Dirent(ctypes.Structure):
    # struct dirent on 64-bit Linux, glibc and musl
    _fields_ = [
        ("d_ino", ctypes.c_uint64),
        ("d_off", ctypes.c_int64),
        ("d_reclen", ctypes.c_ushort),
        ("d_type", ctypes.c_ubyte),
        ("d_name", ctypes.c_char * 256),
    ]


def libc_dir_api():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.opendir.restype = ctypes.c_void_p
    libc.opendir.argtypes = [ctypes.c_char_p]
    libc.readdir.restype = ctypes.POINTER(Dirent)
    libc.readdir.argtypes = [ctypes.c_void_p]
    libc.telldir.restype = ctypes.c_long
    libc.telldir.argtypes = [ctypes.c_void_p]
    libc.seekdir.argtypes = [ctypes.c_void_p, ctypes.c_long]
    libc.closedir.argtypes = [ctypes.c_void_p]
    return libc


@contextmanager
def opened_dir(libc, path):
    d = libc.opendir(path.encode())
    assert d, os.strerror(ctypes.get_errno())
    try:
        yield d
    finally:
        libc.closedir(d)


def read_names(libc, d, limit=None):
    names = []
    while limit is None or len(names) < limit:
        ctypes.set_errno(0)
        ent = libc.readdir(d)
        if not ent:
            assert ctypes.get_errno() == 0, os.strerror(ctypes.get_errno())
            break
        name = ent.contents.d_name.decode()
        if name not in (".", ".."):
            names.append(name)
    return names


@pytest.mark.skipif(not LINUX_LP64, reason="dirent layout is for 64-bit Linux")
@pytest.mark.parametrize("dir_cache", ["yes", "no"])
@pytest.mark.parametrize("sync_readdir", [False, True])
def test_readdir_resumed_on_fresh_handle(tmpdir, dir_cache, sync_readdir):
    """Regression test for upstream PR 379 (assert(offset == 0) in readdir(),
    reported in upstream issues 341 and 338). Linux/libfuse specific.

    seekdir() to a telldir() position on a newly opened directory makes the
    kernel send READDIR with a non-zero offset to a handle that has listed
    nothing yet, as NFS export and Finder do. The listing must resume after
    the entries already read; offset 0 would repeat them.
    """
    count = 2000
    expected = {f"f{i:05d}" for i in range(count)}
    opts = [f"dir_cache={dir_cache}"] + (["sync_readdir"] if sync_readdir else [])
    with mock_mount(tmpdir, {"readdir_batch": 37}, opts) as m:
        for name in expected:
            open(pjoin(m.src, name), "w").close()
        assert set(os.listdir(m.mnt)) == expected

        libc = libc_dir_api()
        with opened_dir(libc, m.mnt) as d:
            first = read_names(libc, d, 800)
            pos = libc.telldir(d)
        with opened_dir(libc, m.mnt) as d:
            libc.seekdir(d, pos)
            rest = read_names(libc, d)

        assert m.proc.poll() is None, "sshfs died"
        assert os.path.ismount(m.mnt)
        assert len(first) == 800 and rest
        assert len(set(rest)) == len(rest)
        assert set(first).isdisjoint(rest)
        assert set(first) | set(rest) == expected


def test_out_of_order_replies(tmpdir):
    """SFTP servers may answer requests in any order."""
    data = os.urandom(262144)
    with mock_mount(tmpdir, {"reorder": True}) as m:
        for i in range(20):
            open(pjoin(m.src, f"n{i}"), "w").close()
        # A large read or write sends several requests at once, which the mock
        # answers newest first. Retry in case a loaded machine spreads them out.
        for i in range(5):
            write_file(pjoin(m.src, f"r{i}"), data)
            assert read_file(pjoin(m.mnt, f"r{i}")) == data
            write_file(pjoin(m.mnt, f"w{i}"), data)
            assert read_file(pjoin(m.src, f"w{i}")) == data
            stats = m.stats()
            if stats.get("reordered_read") and stats.get("reordered_write"):
                break
        assert stats.get("reordered_read", 0) > 0, "mock never reordered READ replies"
        assert stats.get("reordered_write", 0) > 0, "mock never reordered WRITE replies"
        assert len(os.listdir(m.mnt)) == 20 + 2 * (i + 1)


def test_attrs_with_extended_fields(tmpdir):
    """ATTRS carrying extended pairs, as some servers send."""
    config = {"attr_extended": [["acl@example.com", "x" * 300], ["a", ""]]}
    with mock_mount(tmpdir, config) as m:
        write_file(pjoin(m.src, "f"), b"hello")
        assert os.stat(pjoin(m.mnt, "f")).st_size == 5
        assert os.listdir(m.mnt) == ["f"]
        assert read_file(pjoin(m.mnt, "f")) == b"hello"


@pytest.mark.parametrize("kind", ["count", "length"])
def test_malformed_extended_attrs(tmpdir, kind):
    """ATTRS whose extended pairs are cut short are an I/O error."""
    config = {
        "attr_extended": [["acl@example.com", "x"]],
        "attr_extended_bad": {"name": "bad", "kind": kind},
    }
    with mock_mount(tmpdir, config) as m:
        write_file(pjoin(m.src, "bad"), b"hello")
        write_file(pjoin(m.src, "good"), b"hello")
        with pytest.raises(OSError) as exc_info:
            os.stat(pjoin(m.mnt, "bad"))
        assert exc_info.value.errno == errno.EIO
        assert os.stat(pjoin(m.mnt, "good")).st_size == 5


def test_server_without_extensions(tmpdir):
    """No posix-rename, statvfs, hardlink or fsync: the fallbacks."""
    with mock_mount(tmpdir, {"extensions": {}}) as m:
        write_file(pjoin(m.src, "a"), b"a")
        write_file(pjoin(m.src, "b"), b"b")
        assert os.statvfs(m.mnt).f_namemax > 0
        with pytest.raises(OSError) as exc_info:
            os.link(pjoin(m.mnt, "a"), pjoin(m.mnt, "c"))
        # sshfs returns ENOSYS; the kernel may turn that into EPERM
        assert exc_info.value.errno in (errno.ENOSYS, errno.EPERM)
        # Plain SFTP v3 RENAME will not replace an existing file
        with pytest.raises(OSError) as exc_info:
            os.rename(pjoin(m.mnt, "a"), pjoin(m.mnt, "b"))
        assert exc_info.value.errno == errno.EPERM
        fd = os.open(pjoin(m.mnt, "a"), os.O_WRONLY)
        os.write(fd, b"A")
        os.fsync(fd)
        os.close(fd)
        assert read_file(pjoin(m.src, "a")) == b"A"


def test_rename_workaround(tmpdir):
    """workaround=rename makes rename-over work on a plain v3 server."""
    with mock_mount(tmpdir, {"extensions": {}}, ["workaround=rename"]) as m:
        write_file(pjoin(m.src, "a"), b"new")
        write_file(pjoin(m.src, "b"), b"old")
        os.rename(pjoin(m.mnt, "a"), pjoin(m.mnt, "b"))
        assert read_file(pjoin(m.src, "b")) == b"new"
        assert os.listdir(m.src) == ["b"]


def test_connection_drop_without_reconnect(tmpdir):
    """Losing the server unmounts cleanly instead of hanging."""
    with mock_mount(tmpdir, {"drop_on": "trigger"}, exit_code=1) as m:
        write_file(pjoin(m.src, "trigger"), b"")
        with pytest.raises(OSError) as exc_info:
            os.stat(pjoin(m.mnt, "trigger"))
        assert exc_info.value.errno in (errno.EIO, errno.ENOTCONN)
        m.proc.wait(10)  # status 1, checked on exit
        assert not os.path.ismount(m.mnt)
        assert m.stats().get("scripted_disconnect") == 1


def test_connection_drop_with_reconnect(tmpdir):
    """With -o reconnect the next request opens a new connection, and a handle
    from the old one must not be sent: the new server gives the same handle
    name to another file."""
    with mock_mount(tmpdir, {"drop_on": "trigger"}, ["reconnect", "direct_io"]) as m:
        write_file(pjoin(m.src, "trigger"), b"")
        write_file(pjoin(m.src, "old"), b"old data")
        write_file(pjoin(m.src, "new"), b"new data")
        fd_old = os.open(pjoin(m.mnt, "old"), os.O_RDONLY)
        fd_new = -1
        try:
            with pytest.raises(OSError) as exc_info:
                os.stat(pjoin(m.mnt, "trigger"))
            assert exc_info.value.errno == errno.EIO
            # this reconnects, and the mock's first handle is named as the old one was
            fd_new = os.open(pjoin(m.mnt, "new"), os.O_RDONLY)
            with pytest.raises(OSError) as exc_info:
                os.pread(fd_old, 100, 0)
            assert exc_info.value.errno == errno.EIO
            assert os.pread(fd_new, 100, 0) == b"new data"
            assert sorted(os.listdir(m.mnt)) == ["new", "old", "trigger"]
            assert m.connections() == 2
            os.close(fd_new)
            fd_new = -1
            # sshfs_flush() fails a handle from the old connection with EIO,
            # and Linux hands the error of FUSE_FLUSH to close()
            with pytest.raises(OSError) as exc_info:
                os.close(fd_old)
            assert exc_info.value.errno == errno.EIO
            fd_old = -1
        finally:
            for fd in (fd_old, fd_new):
                with suppress(OSError):
                    os.close(fd)
    # after the unmount, so that the releases have been through too
    assert not m.stats().get("unknown_handle"), "sshfs sent a handle from the old connection"
