"""Step 06: false accepts per hour and false-reject rate (JSON report).

Thin wrapper: ``python scripts/06_evaluate.py [options]`` is
``train-wakeword evaluate [options]``.
The logic lives in ``train_wakeword/evaluate.py``; see README.md for the runbook.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from train_wakeword.cli import main

if __name__ == "__main__":
    sys.exit(main(["evaluate", *sys.argv[1:]]))
