#!/usr/bin/env python3
"""Option parsing checks. No sshd needed.

test_rejected_option_combinations exits from main() before fuse_new(), so it
needs no FUSE either. The other test gets as far as fuse_mount() (main()
mounts before it connects), so it carries the FUSE marker.
"""

if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main([__file__] + sys.argv[1:]))

import subprocess
import sys
from os.path import join as pjoin

import pytest

from util import base_cmdline, basename, fuse_test_marker


def run_sshfs(tmpdir, *args):
    res = subprocess.run(
        base_cmdline + [pjoin(basename, "sshfs"), "-f", "host:/dir", str(tmpdir.mkdir("mnt")), *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        text=True,
    )
    sys.stderr.write(res.stderr)  # for conftest's check: sanitizer or valgrind reports
    return res


@pytest.mark.parametrize(
    "opts, message",
    [
        (["-o", "max_conns=0"], "max_conns option must be at least 1"),
        (["-o", "idmap=file"], "need a uidfile or gidfile"),
        (["-o", "passive", "-o", "max_conns=2"], "passive option cannot be specified"),
        (["-o", "password_stdin", "-o", "passive"], "cannot both be specified"),
        (["-o", "workaround=bogus"], "unknown workaround"),
        (["-o", "max_conns=2", "-o", "workaround=buflimit"], "buflimit workaround is not supported"),
    ],
)
def test_rejected_option_combinations(tmpdir, opts, message):
    res = run_sshfs(tmpdir, *opts)
    assert res.returncode == 1
    assert message in res.stderr


@fuse_test_marker()
def test_ssh_argv_ends_with_doubledash_host(tmpdir):
    """The '--' before the host is what stops a host from being parsed as
    an ssh option; keep it there."""
    res = run_sshfs(tmpdir, "-o", "sshfs_debug", "-o", "ssh_command=/dev/null")
    assert res.returncode == 1
    line = next(l for l in res.stderr.splitlines() if l.startswith("executing"))
    assert line.endswith("<--> <host> <sftp>")
