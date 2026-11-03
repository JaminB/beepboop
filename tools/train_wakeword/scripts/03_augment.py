"""Step 03: augment (RIR, noise, gain, pitch) and write feature mmaps.

Thin wrapper: ``python scripts/03_augment.py [options]`` is
``train-wakeword augment [options]``.
The logic lives in ``train_wakeword/augment.py``; see README.md for the runbook.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from train_wakeword.cli import main

if __name__ == "__main__":
    sys.exit(main(["augment", *sys.argv[1:]]))
