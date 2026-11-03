"""Step 01: synthetic positives and TTS near-misses (Piper sample generator).

Thin wrapper: ``python scripts/01_generate_positives.py [options]`` is
``train-wakeword positives [options]``.
The logic lives in ``train_wakeword/positives.py``; see README.md for the runbook.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from train_wakeword.cli import main

if __name__ == "__main__":
    sys.exit(main(["positives", *sys.argv[1:]]))
