"""Initialize the Umbrel bind mount, then leave runtime services unprivileged."""

import os
from pathlib import Path


def initialize(root=Path("/data"), uid=65532, gid=65532):
    for directory in (root, root / "recordings"):
        if directory.is_symlink():
            raise ValueError("Data directories cannot be symbolic links")
        directory.mkdir(parents=True, exist_ok=True)
        os.chown(directory, uid, gid)
        directory.chmod(0o700)


if __name__ == "__main__":
    initialize()
