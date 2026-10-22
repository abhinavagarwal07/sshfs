#!/usr/bin/env python3
"""Check at run time that nothing starts the macFUSE mount before
fuse_daemonize(). No mount is needed: sshfs runs without -f under a small
DYLD shim (daemonize_shim.c) that looks at macFUSE's own "mount started" flag
when sshfs calls fuse_daemonize(). A second test runs sshfs without the shim
to see that it really forks."""

if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main([__file__] + sys.argv[1:]))

import os
import shlex
import subprocess
import time

import pytest
from os.path import join as pjoin
from util import basename, isolated_process, libfuse3_path, run_isolated

SSHFS = pjoin(basename, "sshfs")
SHIM_SRC = pjoin(os.path.dirname(os.path.abspath(__file__)), "daemonize_shim.c")

# Just enough of an SFTP server for sshfs to connect: version reply to INIT,
# a directory for the LSTAT of the remote root, "unsupported" for the rest.
FAKE_SSH = """#!/usr/bin/env python3
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

# What macFUSE prints when fuse_daemonize() refuses to fork, see
# https://github.com/libfuse/sshfs/issues/388
NO_FORK_WARNINGS = (
    "forking after mount is not supported",
    "forking a threaded process is unsafe",
    "continuing in foreground",
)

EXIT_MEANING = {
    86: "the mount was started before fuse_daemonize()",
    87: "sshfs ran in the foreground, so the fork path was not exercised",
    88: "sshfs never reached fuse_daemonize()",
    89: "the shim could not install its watchdog",
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
    lib = libfuse3_path()
    path = str(tmp_path_factory.mktemp("shim") / "daemonize_shim.dylib")
    res = subprocess.run(
        shlex.split(os.environ.get("CC", "cc"))
        + ["-dynamiclib", "-Wall", "-Wextra", "-Werror", "-o", path, SHIM_SRC, lib],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if res.returncode != 0:
        pytest.fail("could not build the shim against %s:\n%s" % (lib, res.stdout))
    return path


def install_fake_ssh(directory):
    """Put the fake SFTP server in directory as fake-ssh."""
    fake_ssh = directory / "fake-ssh"
    fake_ssh.write_text(FAKE_SSH)
    fake_ssh.chmod(0o755)


def sshfs_env(shim=None, helper_dir=None):
    env = dict(os.environ)
    if shim:
        env["DYLD_INSERT_LIBRARIES"] = shim
    if helper_dir:
        # a bare command name: sshfs splits ssh_command at spaces
        env["PATH"] = str(helper_dir) + os.pathsep + env["PATH"]
    return env


def run_sshfs(shim, args, out_file, helper_dir=None, timeout=45):
    rc, output = run_isolated(
        [SSHFS] + args, out_file, timeout, env=sshfs_env(shim, helper_dir)
    )
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
    install_fake_ssh(tmp_path)
    # a file, not a directory: nothing can be mounted there, whatever runs
    mnt_dir = tmp_path / "not-a-directory"
    mnt_dir.write_text("")

    rc, err = run_sshfs(
        shim,
        ["-o", "ssh_command=fake-ssh", "localhost:/", str(mnt_dir)],
        tmp_path / "sshfs.txt",
        helper_dir=tmp_path,
    )
    assert rc == 0, "%s (exit %d)\noutput:\n%s" % (
        EXIT_MEANING.get(rc, "unexpected failure"),
        rc,
        err,
    )
    assert "daemonize-shim: ok: fuse_daemonize(foreground=0)" in err, err


def wait_for_fork(proc, timeout=20):
    """Wait until proc has a child that is sshfs itself, not its ssh helper
    (True), or it exits or time is up (False)."""
    deadline = time.monotonic() + timeout
    while proc.poll() is None and time.monotonic() < deadline:
        if subprocess.run(["pgrep", "-x", "-P", str(proc.pid), "sshfs"],
                          stdout=subprocess.DEVNULL).returncode == 0:
            return True
        time.sleep(0.1)
    return False


def test_real_daemonize(tmp_path):
    """Without the shim, sshfs must fork instead of staying in the foreground.

    A hosted runner cannot finish a mount, so the parent keeps waiting for its
    daemon child to mount rather than returning. The child is the proof that
    fuse_daemonize() forked; if the mount was started first, there is no child
    and sshfs either hangs or warns."""
    install_fake_ssh(tmp_path)
    mnt_dir = tmp_path / "mnt"
    mnt_dir.mkdir()
    out_file = tmp_path / "sshfs.txt"
    cmdline = [SSHFS, "-o", "ssh_command=fake-ssh", "localhost:/", str(mnt_dir)]
    try:
        with isolated_process(cmdline, out_file, sshfs_env(helper_dir=tmp_path)) as proc:
            forked = wait_for_fork(proc)
            rc = proc.poll()
    finally:
        # once the mount works the child leaves the session, so killpg misses it
        subprocess.run(["pkill", "-9", "-f", str(tmp_path)])
    output = out_file.read_text()
    assert not any(w in output for w in NO_FORK_WARNINGS), output
    assert forked or rc == 0, "sshfs neither forked nor returned (rc %s)\noutput:\n%s" % (
        rc,
        output,
    )
