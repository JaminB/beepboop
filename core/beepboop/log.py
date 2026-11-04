"""Core's logging entry point: a re-export of :mod:`beepboop_protocol.log`.

The redacting structlog setup (CLAUDE.md §5, hard rule 5) lives in the shared
``beepboop-protocol`` package so the audio service uses the very same
processor (ADR 0019). Core code keeps importing it from here::

    from beepboop.log import configure_logging, get_logger
"""

from beepboop_protocol.log import (
    REDACTED,
    SENSITIVE_KEY_PARTS,
    configure_logging,
    get_logger,
    is_sensitive_key,
    redact_secrets,
)

__all__ = [
    "REDACTED",
    "SENSITIVE_KEY_PARTS",
    "configure_logging",
    "get_logger",
    "is_sensitive_key",
    "redact_secrets",
]
