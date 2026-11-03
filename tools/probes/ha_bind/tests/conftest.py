"""v1_report.py is a stand-alone stdlib script (it runs on a bare Pi host), not a
workspace package, so make its directory importable for the tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
