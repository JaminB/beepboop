"""Where core keeps its data when no ``--data-dir`` is given.

In the container (OPERATIONS §3) the data root is ``/data``. On a developer machine the
same default would silently create ``/data`` (or ``C:\\data`` on Windows), so outside the
container the default is a repo-local ``.data/`` under the current directory. The
precedence is ``BB_DATA_DIR`` > ``/data`` inside a container > ``./.data``.
"""

from __future__ import annotations

import os
from pathlib import Path

CONTAINER_DATA_DIR = Path("/data")
DEV_DATA_DIR = Path(".data")


def in_container() -> bool:
    """True inside the core image (``/.dockerenv`` or ``BB_IN_CONTAINER=1``)."""
    return os.environ.get("BB_IN_CONTAINER") == "1" or Path("/.dockerenv").exists()


def default_data_dir(env: dict[str, str] | None = None) -> Path:
    e = env if env is not None else dict(os.environ)
    if e.get("BB_DATA_DIR"):
        return Path(e["BB_DATA_DIR"])
    if in_container() or (os.name != "nt" and CONTAINER_DATA_DIR.is_dir()):
        return CONTAINER_DATA_DIR
    return Path.cwd() / DEV_DATA_DIR


__all__ = ["CONTAINER_DATA_DIR", "DEV_DATA_DIR", "default_data_dir", "in_container"]
