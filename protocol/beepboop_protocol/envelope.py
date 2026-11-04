"""The event envelope and the field types shared by both directions.

ARCHITECTURE §4.1 is the spec. Every event on the wire is one JSON object::

    {"type": "stt.final", "seq": 418, "ts": 1793657612.482, "turn_id": "01JB...", "data": {...}}

Each concrete event is a subclass of :class:`EventBase` that pins ``type`` to a
``Literal`` and declares a typed ``data`` model. Two class variables describe
the event for the rest of the package:

* ``CANONICAL``: one of the eight turn events that may carry turn content.
* ``TURN_SCOPE``: whether the envelope ``turn_id`` is ``required`` (turn
  events), ``optional`` (events that may belong to a turn, such as
  ``speak.done``) or ``forbidden`` (pure control plane, such as ``health``).

Validation choices that apply to every model:

* ``strict``: no silent coercion (``"5"`` is not an int, ``1`` is not a bool).
* ``extra="ignore"``: unknown ``data`` fields are ignored (§4.1).
* ``allow_inf_nan=False``: JSON has no NaN/Infinity, and a NaN score is a bug.
* ``frozen``: events are values; the sender stamps ``seq`` on a copy.

This module imports only the standard library and pydantic, so core can use
the protocol without pulling in any audio dependency.
"""

from __future__ import annotations

import secrets
import time
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Protocol major version sent in ``hello``. A mismatch refuses the connection.
#: 2 since the embedding length became per-connection (ADR 0019); 1 was never released.
PROTOCOL_MAJOR = 2

#: Upper bound on any embedding length (ARCHITECTURE §4.4). The actual length is
#: per connection: audio announces it in ``hello.speaker.dim`` and
#: :class:`~.connection.EventChannel` checks every embedding against it.
MAX_EMBEDDING_DIM = 4096

#: The v0.1 speaker model's embedding length (CAM++, VOICE_PIPELINE §8.1). Only
#: examples and tests use it; nothing on the wire assumes it.
DEFAULT_EMBEDDING_DIM = 512

#: The fixed id of the built-in guest profile (ARCHITECTURE §4.2, IDENTITY_AND_TRUST §2).
GUEST_PROFILE_ID = "guest"

#: Crockford base32 ULID: 26 chars, first char ``0``-``7`` (48-bit timestamp).
ULID_PATTERN = r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$"
_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

TurnScope = Literal["required", "optional", "forbidden"]


def new_ulid(now_ms: int | None = None) -> str:
    """Return a new ULID (48-bit Unix ms timestamp + 80 random bits).

    Audio mints a ``turn_id`` with this at ``wake`` (VOICE_PIPELINE §5).
    """
    ms = int(time.time() * 1000) if now_ms is None else now_ms
    if not 0 <= ms < 2**48:
        raise ValueError("ULID timestamp out of range")
    value = (ms << 80) | int.from_bytes(secrets.token_bytes(10), "big")
    return "".join(_ULID_ALPHABET[(value >> shift) & 0x1F] for shift in range(125, -1, -5))


#: A profile id: a ULID (``profiles.id``) or exactly ``"guest"``, never ``"GUEST"``
#: (ARCHITECTURE §4.2, §4.4).
ProfileId = Annotated[str, Field(pattern=rf"^(?:{GUEST_PROFILE_ID}|{ULID_PATTERN[1:-1]})$")]

#: A non-empty short identifier (utterance ids, session ids, provider names).
Name = Annotated[str, Field(min_length=1, max_length=128)]

#: Audio-side ``time.monotonic_ns()`` capture stamp (VOICE_PIPELINE §3).
MonotonicNs = Annotated[int, Field(ge=0)]

#: A non-negative duration in milliseconds.
Millis = Annotated[int, Field(ge=0)]

#: A BCP 47 locale tag such as ``en-US``.
Locale = Annotated[str, Field(min_length=2, max_length=35, pattern=r"^[A-Za-z0-9-]+$")]


#: One speaker embedding: finite floats, as many as the connection's
#: ``hello.speaker.dim`` (checked per connection, not here). Numbers, not audio,
#: so it may cross the socket in both directions (ARCHITECTURE §3).
Embedding = Annotated[list[float], Field(min_length=1, max_length=MAX_EMBEDDING_DIM)]


class WireModel(BaseModel):
    """Base for every model that crosses the socket."""

    model_config = ConfigDict(
        strict=True,
        extra="ignore",
        frozen=True,
        allow_inf_nan=False,
    )


def _now() -> float:
    return round(time.time(), 3)


class EventBase(WireModel):
    """The envelope fields shared by every event (ARCHITECTURE §4.1).

    ``seq`` and ``ts`` belong to the sender: :class:`~.connection.EventChannel`
    stamps ``seq`` on send, so callers leave it at ``0``.
    """

    CANONICAL: ClassVar[bool] = False
    TURN_SCOPE: ClassVar[TurnScope] = "forbidden"

    #: Event name; every subclass narrows it to a ``Literal`` (the union discriminator).
    type: str
    seq: int = Field(default=0, ge=0)
    ts: float = Field(default_factory=_now, ge=0)
    turn_id: str | None = Field(default=None, pattern=ULID_PATTERN)

    @field_validator("ts")
    @classmethod
    def _ms_precision(cls, value: float) -> float:
        return round(value, 3)

    def embedding_lengths(self) -> tuple[int, ...]:
        """Lengths of every speaker embedding this event carries (none by default).

        The channel compares them with the negotiated ``hello.speaker.dim``.
        """
        return ()

    @model_validator(mode="after")
    def _check_turn_scope(self) -> EventBase:
        if self.TURN_SCOPE == "required" and self.turn_id is None:
            raise ValueError(f"{self.type} is turn-scoped and needs a turn_id")
        if self.TURN_SCOPE == "forbidden" and self.turn_id is not None:
            raise ValueError(f"{self.type} is a control-plane event and carries no turn_id")
        return self
