#!/usr/bin/env python3
"""Mount options that other tools and older guides hand to sshfs."""

if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main([__file__] + sys.argv[1:]))

import os
import shlex
import shutil
import subprocess
import sys
from contextlib import contextmanager
from os.path import join as pjoin
from util import (
    wait_for_mount,
    umount,
    cleanup,
    base_cmdline,
    basename,
    fuse_test_marker,
)

pytestmark = fuse_test_marker()

KNOWN_HOSTS_NOISE = r"^Warning: Permanently added 'localhost' .+"


@contextmanager
def sshfs_mounted(src_dir, mnt_dir, opts):
    """Mount localhost:src_dir on mnt_dir with one -o argument per entry of opts."""
    cmdline = base_cmdline + [
        pjoin(basename, "sshfs"),
        "-f",
        f"localhost:{src_dir}",
        mnt_dir,
        "-o",
        "StrictHostKeyChecking=no,BatchMode=yes",
    ]
    for opt in opts:
        cmdline += ["-o", opt]
    new_env = dict(os.environ)
    new_env["G_DEBUG"] = "fatal-warnings"
    mount_process = subprocess.Popen(cmdline, env=new_env)
    try:
        wait_for_mount(mount_process, mnt_dir)
        yield
        umount(mount_process, mnt_dir)
    except BaseException:
        cleanup(mount_process, mnt_dir)
        raise


def captured_stderr(capfd):
    """Return what the mount wrote to stderr, and put it back for conftest's check."""
    captured = capfd.readouterr()
    sys.stdout.write(captured.out)
    sys.stderr.write(captured.err)
    return captured.err


def test_obsolete_libfuse2_options(tmpdir, capfd):
    """nonempty, big_writes and large_read are no-ops in libfuse 3, not errors."""

    capfd.register_output(KNOWN_HOSTS_NOISE, count=0)
    mnt_dir = str(tmpdir.mkdir("mnt"))
    src_dir = str(tmpdir.mkdir("src"))
    # nonempty used to be what allowed mounting over a populated directory
    with open(pjoin(mnt_dir, "underneath"), "wb") as fh:
        fh.write(b"hidden by the mount")
    with open(pjoin(src_dir, "remote"), "wb") as fh:
        fh.write(b"served by the mount")

    # a single comma separated -o, like mount(8) builds from /etc/fstab
    with sshfs_mounted(src_dir, mnt_dir, ["nonempty,big_writes,large_read"]):
        assert os.listdir(mnt_dir) == ["remote"]
        with open(pjoin(mnt_dir, "remote"), "rb") as fh:
            assert fh.read() == b"served by the mount"

    err = captured_stderr(capfd)
    for name in ("nonempty", "big_writes", "large_read"):
        assert f"option '{name}' is obsolete and ignored" in err


def test_fstab_nofail(tmpdir, capfd):
    """mount(8) hands nofail to the helper; it means nothing to sshfs."""

    capfd.register_output(KNOWN_HOSTS_NOISE, count=0)
    mnt_dir = str(tmpdir.mkdir("mnt"))
    src_dir = str(tmpdir.mkdir("src"))
    with open(pjoin(src_dir, "remote"), "wb") as fh:
        fh.write(b"served by the mount")

    with sshfs_mounted(src_dir, mnt_dir, ["nofail,_netdev"]):
        assert os.listdir(mnt_dir) == ["remote"]

    assert "nofail" not in captured_stderr(capfd)


def test_ssh_accepted_algorithms_options(tmpdir, capfd, monkeypatch):
    """The OpenSSH 8.5 names of the key type options reach ssh."""

    capfd.register_output(KNOWN_HOSTS_NOISE, count=0)
    mnt_dir = str(tmpdir.mkdir("mnt"))
    src_dir = str(tmpdir.mkdir("src"))
    with open(pjoin(src_dir, "remote"), "wb") as fh:
        fh.write(b"served by the mount")

    # A stand-in "ssh" first in PATH that records what sshfs asked for. It
    # does not hand the two options on, so the test does not depend on the
    # OpenSSH version.
    real_ssh = shutil.which("ssh")
    bin_dir = str(tmpdir.mkdir("stand-in ssh"))  # the space is deliberate
    arg_log = pjoin(bin_dir, "ssh-args")
    with open(pjoin(bin_dir, "ssh"), "w") as fh:
        fh.write("#!/bin/sh\n")
        fh.write(f"printf '%s\\n' \"$@\" > {shlex.quote(arg_log)}\n")
        fh.write('for arg in "$@"; do\n')
        fh.write("    shift\n")
        fh.write("    case $arg in\n")
        fh.write("    -oPubkeyAcceptedAlgorithms=*) ;;\n")
        fh.write("    -oHostbasedAcceptedAlgorithms=*) ;;\n")
        fh.write('    *) set -- "$@" "$arg" ;;\n')
        fh.write("    esac\n")
        fh.write("done\n")
        fh.write(f'exec {shlex.quote(real_ssh)} "$@"\n')
    os.chmod(pjoin(bin_dir, "ssh"), 0o755)
    monkeypatch.setenv("PATH", bin_dir + os.pathsep + os.environ["PATH"])

    opts = [
        "PubkeyAcceptedAlgorithms=+ssh-rsa",
        "HostbasedAcceptedAlgorithms=+ssh-rsa",
    ]
    with sshfs_mounted(src_dir, mnt_dir, opts):
        assert os.listdir(mnt_dir) == ["remote"]

    with open(arg_log) as fh:
        ssh_args = fh.read().splitlines()
    # they have to be ssh options, not part of the destination
    end_of_options = ssh_args.index("--")
    for opt in (
        "-oPubkeyAcceptedAlgorithms=+ssh-rsa",
        "-oHostbasedAcceptedAlgorithms=+ssh-rsa",
    ):
        assert ssh_args.count(opt) == 1
        assert ssh_args.index(opt) < end_of_options
