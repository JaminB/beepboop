"""structlog setup for the training scripts (CLAUDE.md §5: no ``print``).

Console rendering by default because these scripts are run by a person at a
terminal; ``TRAIN_WAKEWORD_LOG=json`` switches to JSON lines.
"""

from __future__ import annotations

import logging
import os

import structlog


def configure_logging(level: int = logging.INFO) -> None:
    """Configure structlog once for the whole pipeline."""
    renderer: structlog.types.Processor
    if os.environ.get("TRAIN_WAKEWORD_LOG", "console") == "json":
        renderer = structlog.processors.JSONRenderer(sort_keys=True)
    else:
        renderer = structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%H:%M:%S"),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger for ``name``."""
    return structlog.get_logger(name)
