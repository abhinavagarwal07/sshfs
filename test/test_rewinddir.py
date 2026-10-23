"""An open directory must survive rename when rewinddir refreshes its listing."""

from pathlib import Path
import subprocess

import pytest

from test_sshfs import _mount_sshfs
from util import cleanup, fuse_test_marker, umount

pytestmark = fuse_test_marker()


@pytest.mark.parametrize("dir_cache", ["yes", "no"])
@pytest.mark.parametrize("sync_readdir", [False, True])
def test_rewinddir_after_rename(tmpdir, dir_cache, sync_readdir, capfd):
    capfd.register_output(r"^Warning: Permanently added 'localhost' .+", count=0)
    options = [f"dir_cache={dir_cache}"]
    if sync_readdir:
        options.append("sync_readdir")
    process, mnt_dir, src_dir = _mount_sshfs(tmpdir, options)
    try:
        entries = Path(src_dir) / "entries"
        entries.mkdir()
        # Long names force enumeration across several libc and FUSE buffers.
        for i in range(2048):
            (entries / (f"file-{i:06d}-" + "0" * 160)).touch()
        subprocess.run(
            [str(Path(__file__).with_name("rewinddir")),
             str(Path(mnt_dir) / "entries")],
            check=True, timeout=30,
        )
    except BaseException:
        cleanup(process, mnt_dir)
        raise
    else:
        umount(process, mnt_dir)
