"""deploy/host is a single stdlib script run by the Pi's own Python, not a package."""

from __future__ import annotations

import sys
from pathlib import Path

HOST_DIR = Path(__file__).resolve().parents[1] / "host"
if str(HOST_DIR) not in sys.path:
    sys.path.insert(0, str(HOST_DIR))
