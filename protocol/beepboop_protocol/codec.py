"""JSON-lines framing: encode, decode and a byte-stream line splitter.

ARCHITECTURE §4.1: one UTF-8 JSON object per ``\\n``-terminated line, no binary
payloads. Rules implemented here:

* Unknown ``type`` values are ignored and logged at debug level: ``decode``
  returns ``None``. An event the *other* side sends but this side never
  receives (``speak`` arriving at core) counts as unknown, too.
* Unknown ``data`` fields are ignored (the models use ``extra="ignore"``).
* A known type that fails validation raises :class:`EventValidationError`.
  Its message lists field locations and error kinds but never input values,
  so a bad ``capture_pin`` cannot leak its hash into a log line.
* Lines longer than :data:`MAX_LINE_BYTES` are refused on encode and dropped
  (up to the next newline) on decode. The spec sets no limit; 1 MiB holds a
  ``profiles.sync`` for dozens of profiles with several centroids each.

Everything here is sans-IO; :mod:`.connection` wires it to sockets and queues.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import cast

import structlog
from pydantic import TypeAdapter, ValidationError

from beepboop_protocol.audio_events import AUDIO_EVENT_CLASSES, AudioEvent
from beepboop_protocol.core_events import CORE_EVENT_CLASSES, CoreEvent
from beepboop_protocol.envelope import EventBase

log = structlog.get_logger(__name__)

#: Longest accepted line, in bytes, excluding the terminating newline.
MAX_LINE_BYTES = 1024 * 1024


class ProtocolError(Exception):
    """Base for every error raised by the event protocol."""


class FrameError(ProtocolError):
    """A line is not a JSON object with a string ``type``, or is too long."""


class EventValidationError(ProtocolError):
    """A line names a known event type but its fields are invalid."""

    def __init__(self, event_type: str, errors: list[str]) -> None:
        self.event_type = event_type
        self.errors = errors
        super().__init__(f"invalid {event_type} event: {'; '.join(errors)}")


def encode(event: EventBase) -> bytes:
    """Serialize ``event`` to one ``\\n``-terminated UTF-8 JSON line.

    ``None`` fields are omitted, so an absent ``turn_id`` or optional field is
    absent on the wire rather than ``null``.
    """
    body = event.model_dump_json(exclude_none=True).encode("utf-8")
    if len(body) > MAX_LINE_BYTES:
        raise FrameError(f"{event.type} event is {len(body)} bytes; limit is {MAX_LINE_BYTES}")
    return body + b"\n"


def _summarize(exc: ValidationError) -> list[str]:
    # include_input=False: never echo values (hashes, transcripts) into logs.
    return [
        f"{'.'.join(str(part) for part in err['loc']) or '<root>'}: {err['type']}"
        for err in exc.errors(include_url=False, include_input=False, include_context=False)
    ]


def _decoder[E: EventBase](
    adapter: TypeAdapter[E], classes: tuple[type[EventBase], ...]
) -> Callable[[bytes | str], E | None]:
    known = frozenset(cls.model_fields["type"].default for cls in classes)

    def decode(line: bytes | str) -> E | None:
        try:
            raw: object = json.loads(line)
        except (ValueError, UnicodeDecodeError) as exc:
            raise FrameError(f"line is not valid UTF-8 JSON: {exc.__class__.__name__}") from None
        if not isinstance(raw, dict):
            raise FrameError("line is not a JSON object")
        event_type = cast("dict[str, object]", raw).get("type")
        if not isinstance(event_type, str):
            raise FrameError("event has no string 'type'")
        if event_type not in known:
            log.debug("event.unknown_type", type=event_type)
            return None
        try:
            event = adapter.validate_json(line)
        except ValidationError as exc:
            raise EventValidationError(event_type, _summarize(exc)) from None
        return event

    return decode


_AUDIO_ADAPTER: TypeAdapter[AudioEvent] = TypeAdapter(AudioEvent)
_CORE_ADAPTER: TypeAdapter[CoreEvent] = TypeAdapter(CoreEvent)

#: Decode a line that audio sent (used by core). ``None`` = unknown type, ignored.
decode_audio_event: Callable[[bytes | str], AudioEvent | None] = _decoder(
    _AUDIO_ADAPTER, AUDIO_EVENT_CLASSES
)

#: Decode a line that core sent (used by audio). ``None`` = unknown type, ignored.
decode_core_event: Callable[[bytes | str], CoreEvent | None] = _decoder(
    _CORE_ADAPTER, CORE_EVENT_CLASSES
)


class LineFramer:
    """Split a byte stream into lines, enforcing the maximum line length.

    Feed it whatever ``read()`` returned; it returns the complete, non-blank
    lines (without the newline). A line that grows past ``max_line_bytes`` is
    discarded up to its terminating newline and counted in ``oversized``; the
    stream then resynchronizes on the next line.
    """

    def __init__(self, max_line_bytes: int = MAX_LINE_BYTES) -> None:
        self.max_line_bytes = max_line_bytes
        self.oversized = 0
        self._buf = bytearray()
        self._discarding = False

    def feed(self, data: bytes) -> list[bytes]:
        lines: list[bytes] = []
        start = 0
        while True:
            end = data.find(b"\n", start)
            chunk = data[start:] if end == -1 else data[start:end]
            if self._discarding:
                pass
            elif len(self._buf) + len(chunk) > self.max_line_bytes:
                self._drop()
            else:
                self._buf += chunk
            if end == -1:
                return lines
            if not self._discarding:
                line = bytes(self._buf).rstrip(b"\r")
                if line.strip():
                    lines.append(line)
            self._buf.clear()
            self._discarding = False
            start = end + 1

    def eof(self) -> None:
        """Signal end of stream; an unterminated trailing line is dropped and logged."""
        if self._buf or self._discarding:
            log.warning("event.unterminated_line", bytes=len(self._buf))
        self._buf.clear()
        self._discarding = False

    def _drop(self) -> None:
        self._discarding = True
        self._buf.clear()
        self.oversized += 1
        log.warning("event.line_too_long", limit=self.max_line_bytes)


__all__ = [
    "MAX_LINE_BYTES",
    "EventValidationError",
    "FrameError",
    "LineFramer",
    "ProtocolError",
    "decode_audio_event",
    "decode_core_event",
    "encode",
]
