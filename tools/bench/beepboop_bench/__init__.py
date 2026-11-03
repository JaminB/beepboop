"""beepboop bench: the concurrent pipeline benchmark and the M0 gates.

Reached as ``beepboop bench <sub>`` (core mounts :data:`beepboop_bench.cli.app`).
Subcommands and what each reports: OPERATIONS §11. Gate thresholds: the M0
table in ROADMAP.md, the only place they live.
"""

from importlib import metadata

try:
    __version__ = metadata.version("beepboop-bench")
except metadata.PackageNotFoundError:  # running from a source tree without install
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
