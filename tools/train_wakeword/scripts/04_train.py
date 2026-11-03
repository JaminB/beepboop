"""Step 04: write the trainer YAML and run microWakeWord's model_train_eval.

Thin wrapper: ``python scripts/04_train.py [options]`` is
``train-wakeword train [options]``.
The logic lives in ``train_wakeword/training.py``; see README.md for the runbook.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from train_wakeword.cli import main

if __name__ == "__main__":
    sys.exit(main(["train", *sys.argv[1:]]))
