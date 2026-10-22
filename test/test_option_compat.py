#!/usr/bin/env python3
"""Mount options that other tools and older guides hand to sshfs."""

if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main([__file__] + sys.argv[1:]))

import os
import subprocess
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
        "StrictHostKeyChecking=no",
    ]
    for opt in opts:
        cmdline += ["-o", opt]
    new_env = dict(os.environ)
    new_env["G_DEBUG"] = "fatal-warnings"
    mount_process = subprocess.Popen(cmdline, env=new_env)
    try:
        wait_for_mount(mount_process, mnt_dir)
        yield
    except Exception:
        cleanup(mount_process, mnt_dir)
        raise
    else:
        umount(mount_process, mnt_dir)


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

    err = capfd.readouterr().err
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

    assert "nofail" not in capfd.readouterr().err


def test_ssh_accepted_algorithms_options(tmpdir, capfd):
    """The OpenSSH 8.5 names of the key type options reach ssh."""

    capfd.register_output(KNOWN_HOSTS_NOISE, count=0)
    mnt_dir = str(tmpdir.mkdir("mnt"))
    src_dir = str(tmpdir.mkdir("src"))
    with open(pjoin(src_dir, "remote"), "wb") as fh:
        fh.write(b"served by the mount")

    # stand-in for ssh that records what sshfs asked for
    arg_log = str(tmpdir.join("ssh-args"))
    wrapper = str(tmpdir.join("ssh-wrapper"))
    with open(wrapper, "w") as fh:
        fh.write("#!/bin/sh\n")
        fh.write(f"printf '%s\\n' \"$@\" > '{arg_log}'\n")
        fh.write('exec ssh "$@"\n')
    os.chmod(wrapper, 0o755)

    opts = [
        f"ssh_command={wrapper}",
        "PubkeyAcceptedAlgorithms=+ssh-rsa",
        "HostbasedAcceptedAlgorithms=+ssh-rsa",
    ]
    with sshfs_mounted(src_dir, mnt_dir, opts):
        assert os.listdir(mnt_dir) == ["remote"]

    with open(arg_log) as fh:
        ssh_args = fh.read().splitlines()
    assert "-oPubkeyAcceptedAlgorithms=+ssh-rsa" in ssh_args
    assert "-oHostbasedAcceptedAlgorithms=+ssh-rsa" in ssh_args
