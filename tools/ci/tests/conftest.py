"""Make the stand-alone scripts in tools/ci importable from their tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
