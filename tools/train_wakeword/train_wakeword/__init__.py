"""Reproducible training pipeline for the stock "Hey beepboop" microWakeWord model.

Steps (``python -m train_wakeword <step>`` or ``scripts/0N_*.py``):

1. ``positives``  Piper synthetic clips, many voices, phrase variants, speed variation.
2. ``negatives``  Download the documented negative datasets (licences in DATA_LICENSES.md).
3. ``augment``    RIR, background noise, gain, pitch; write microWakeWord feature mmaps.
4. ``train``      Write the trainer YAML and run ``microwakeword.model_train_eval``.
5. ``export``     Copy the quantised streaming TFLite and write the manifest JSON.
6. ``evaluate``   False accepts/hour and false-reject rate on held-out data (JSON).

Only ``config``, ``manifest``, ``licenses`` and ``cli`` are importable without the
training dependencies; everything that touches TensorFlow, microwakeword or Piper is
imported lazily so the x86 smoke tests run in the workspace venv.
"""

__version__ = "0.1.0"
