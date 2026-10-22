#!/usr/bin/env python3
"""Check at run time that nothing starts the macFUSE mount before
fuse_daemonize(). No mount is needed: sshfs runs without -f under a small
DYLD shim (daemonize_shim.c) that looks at macFUSE's own "mount started" flag
when sshfs calls fuse_daemonize()."""

if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main([__file__] + sys.argv[1:]))

import os
import re
import signal
import subprocess
import sys

import pytest
from os.path import join as pjoin
from util import basename

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")

SSHFS = pjoin(basename, "sshfs")
SHIM_SRC = pjoin(os.path.dirname(os.path.abspath(__file__)), "daemonize_shim.c")

# Just enough of an SFTP server for sshfs to connect: version reply to INIT,
# a directory for the LSTAT of the remote root, "unsupported" for the rest.
FAKE_SSH = """#!%s
import struct, sys

def read(n):
    data = b""
    while len(data) < n:
        chunk = sys.stdin.buffer.read(n - len(data))
        if not chunk:
            sys.exit(0)
        data += chunk
    return data

def send(kind, payload):
    sys.stdout.buffer.write(struct.pack(">IB", len(payload) + 1, kind) + payload)
    sys.stdout.buffer.flush()

while True:
    (size,) = struct.unpack(">I", read(4))
    pkt = read(size)
    if pkt[0] == 1:    # INIT
        send(2, struct.pack(">I", 3))
    elif pkt[0] == 7:  # LSTAT
        send(105, pkt[1:5] + struct.pack(">II", 4, 0o40755))
    else:
        send(101, pkt[1:5] + struct.pack(">I", 8) + struct.pack(">II", 0, 0))
"""

EXIT_MEANING = {
    85: "libfuse3 does not export fuse_darwin_mount_started",
    86: "the mount was started before fuse_daemonize()",
    87: "sshfs ran in the foreground, so the fork path was not exercised",
    88: "sshfs never reached fuse_daemonize()",
}


def signature():
    res = subprocess.run(
        ["codesign", "-dv", "--verbose=4", SSHFS],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return res.stdout


@pytest.fixture(scope="module")
def shim(tmp_path_factory):
    linked = subprocess.run(
        ["otool", "-L", SSHFS], stdout=subprocess.PIPE, text=True, check=True
    ).stdout
    m = re.search(r"^\s*(\S*libfuse3\S*\.dylib)", linked, re.M)
    if not m:
        pytest.fail("sshfs is not linked against libfuse3:\n" + linked)

    path = str(tmp_path_factory.mktemp("shim") / "daemonize_shim.dylib")
    res = subprocess.run(
        [os.environ.get("CC", "cc"), "-dynamiclib", "-Wall", "-Wextra", "-Werror",
         "-o", path, SHIM_SRC, m.group(1)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if res.returncode != 0:
        pytest.fail("could not build the shim against %s:\n%s" % (m.group(1), res.stdout))
    return path


def run_sshfs(shim, args, out_file, timeout=40):
    """Run sshfs and return (exit status, its output).

    The output goes to a file, not a pipe: a failed mount start can leave
    helper processes that keep a pipe open after sshfs itself is gone."""
    env = dict(os.environ, DYLD_INSERT_LIBRARIES=shim)
    with open(out_file, "w") as out:
        proc = subprocess.Popen(
            [SSHFS] + args,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            rc = None
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
    output = open(out_file).read()
    if rc is None:
        pytest.fail("sshfs did not exit in %ds\noutput:\n%s" % (timeout, output))
    return rc, output


def test_shim_loads(shim, tmp_path):
    """DYLD_INSERT_LIBRARIES must work for the sshfs built here; if it does
    not (hardened runtime, SIP), the check below would prove nothing."""
    rc, err = run_sshfs(shim, ["--version"], tmp_path / "version.txt")
    assert "daemonize-shim: loaded" in err, (
        "shim was not loaded\nsignature:\n%s\noutput:\n%s" % (signature(), err)
    )
    assert rc == 0, err


def test_no_mount_before_daemonize(shim, tmp_path):
    fake_ssh = tmp_path / "fake-ssh"
    fake_ssh.write_text(FAKE_SSH % sys.executable)
    fake_ssh.chmod(0o755)
    mnt_dir = tmp_path / "mnt"
    mnt_dir.mkdir()

    rc, err = run_sshfs(
        shim,
        ["-o", "ssh_command=" + str(fake_ssh), "localhost:/", str(mnt_dir)],
        tmp_path / "sshfs.txt",
    )
    assert rc == 0, "%s (exit %d)\noutput:\n%s" % (
        EXIT_MEANING.get(rc, "unexpected failure"),
        rc,
        err,
    )
    assert "daemonize-shim: ok: fuse_daemonize(foreground=0)" in err, err
