"""Step 05: export the TFLite model, the manifest JSON and provenance.

Thin wrapper: ``python scripts/05_export.py [options]`` is
``train-wakeword export [options]``.
The logic lives in ``train_wakeword/export.py``; see README.md for the runbook.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from train_wakeword.cli import main

if __name__ == "__main__":
    sys.exit(main(["export", *sys.argv[1:]]))
