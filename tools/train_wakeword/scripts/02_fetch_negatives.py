"""Step 02: download and convert the negative, background and RIR datasets.

Thin wrapper: ``python scripts/02_fetch_negatives.py [options]`` is
``train-wakeword negatives [options]``.
The logic lives in ``train_wakeword/negatives.py``; see README.md for the runbook.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from train_wakeword.cli import main

if __name__ == "__main__":
    sys.exit(main(["negatives", *sys.argv[1:]]))
