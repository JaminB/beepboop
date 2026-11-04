"""structlog configuration shared by core, the audio service and the tools.

It lives in ``beepboop-protocol`` (ADR 0019) because both services need the
same redaction: audio captures spoken PINs, core holds tokens. Core code
imports it through the ``beepboop.log`` re-export; audio imports it from here.

CLAUDE.md §5: JSON lines to stdout, no ``print``, no f-strings in log calls,
``turn_id`` on every line inside a turn (bind it with
``structlog.contextvars.bind_contextvars(turn_id=...)``).

CLAUDE.md hard rule 5: any key whose name contains ``secret``, ``token``,
``password`` or ``pin`` (case-insensitive, at any nesting depth) is replaced
with ``REDACTED`` before rendering. The match is a deliberate substring match,
so it over-redacts (``mapping`` contains ``pin``); that is the safe direction.

Usage::

    from beepboop_protocol.log import configure_logging, get_logger

    configure_logging()
    log = get_logger(__name__)
    log.info("tool.called", tool="home.turn_on")
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping
from typing import TextIO, cast

import structlog
from structlog.types import EventDict, Processor, WrappedLogger

#: Substrings that mark a key as sensitive (hard rule 5).
SENSITIVE_KEY_PARTS: tuple[str, ...] = ("secret", "token", "password", "pin")

#: Replacement value for sensitive fields.
REDACTED = "[REDACTED]"


def is_sensitive_key(key: str) -> bool:
    """True if ``key`` names a secret, token, password or PIN."""
    lowered = key.lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def _redact(value: object) -> object:
    """Recursively redact sensitive keys inside mappings, lists and tuples."""
    if isinstance(value, Mapping):
        mapping = cast("Mapping[object, object]", value)
        return {
            key: REDACTED if isinstance(key, str) and is_sensitive_key(key) else _redact(item)
            for key, item in mapping.items()
        }
    if isinstance(value, list | tuple):
        items = [_redact(item) for item in cast("list[object] | tuple[object, ...]", value)]
        return items if isinstance(value, list) else tuple(items)
    return value


def redact_secrets(_logger: WrappedLogger, _method_name: str, event_dict: EventDict) -> EventDict:
    """structlog processor implementing hard rule 5."""
    return {
        key: (REDACTED if is_sensitive_key(key) else _redact(value))
        for key, value in event_dict.items()
    }


def configure_logging(level: int | str = logging.INFO, stream: TextIO | None = None) -> None:
    """Configure structlog to emit redacted JSON lines on ``stream`` (stdout by default)."""
    if isinstance(level, str):
        level = logging.getLevelNamesMapping()[level.upper()]
    processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact_secrets,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.dict_tracebacks,
        structlog.processors.JSONRenderer(sort_keys=True),
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=stream or sys.stdout),
        cache_logger_on_first_use=False,
    )


class _NamedLazyLogger:
    """Resolve structlog's *current* config on every call, then bind ``logger=name``.

    ``structlog.get_logger().bind(...)`` would build the logger immediately, freezing
    whatever config exists at that moment; for a module-level logger that is
    structlog's default config, without redaction. (``structlog.get_logger(logger=...)``
    is not an option: ``logger`` collides with ``wrap_logger``'s first parameter.)
    """

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attr: str) -> object:
        bound: structlog.typing.FilteringBoundLogger = structlog.get_logger()
        return getattr(bound.bind(logger=self._name), attr)


def get_logger(name: str | None = None) -> structlog.typing.FilteringBoundLogger:
    """Return a lazy logger; ``name`` is attached as ``logger``.

    Configuration is resolved at each log call, so a module-level
    ``log = get_logger(__name__)`` is safe even though it runs before
    ``configure_logging()``.
    """
    if name is None:
        log: structlog.typing.FilteringBoundLogger = structlog.get_logger()
        return log
    return cast("structlog.typing.FilteringBoundLogger", _NamedLazyLogger(name))
