"""Minimal ULID ids (DATA_AND_PRIVACY §6: "ids are ULID TEXT").

A ULID is 128 bits: a 48-bit Unix-millisecond timestamp followed by 80 random
bits, written as 26 Crockford base32 characters. The secrets envelope carries a
DEK's id as its 16 raw bytes, so both forms are exposed here.

This is a private helper until ``core/beepboop/db/`` provides the shared one.
"""

from __future__ import annotations

import secrets
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DECODE = {char: index for index, char in enumerate(_ALPHABET)}


def new_ulid_bytes(now_ms: int | None = None) -> bytes:
    """Return a fresh ULID as 16 bytes (big-endian timestamp, then randomness)."""
    timestamp = int(time.time() * 1000) if now_ms is None else now_ms
    return timestamp.to_bytes(6, "big") + secrets.token_bytes(10)


def ulid_to_str(raw: bytes) -> str:
    """Encode 16 ULID bytes as the canonical 26-character string."""
    if len(raw) != 16:
        msg = f"a ULID is 16 bytes, got {len(raw)}"
        raise ValueError(msg)
    value = int.from_bytes(raw, "big")
    return "".join(_ALPHABET[(value >> shift) & 0x1F] for shift in range(125, -1, -5))


def ulid_from_str(text: str) -> bytes:
    """Decode a 26-character ULID string to its 16 bytes."""
    if len(text) != 26:
        msg = f"a ULID string is 26 characters, got {len(text)}"
        raise ValueError(msg)
    value = 0
    for char in text.upper():
        if char not in _DECODE:
            msg = f"invalid ULID character {char!r}"
            raise ValueError(msg)
        value = (value << 5) | _DECODE[char]
    if value >> 128:
        msg = "ULID out of range"
        raise ValueError(msg)
    return value.to_bytes(16, "big")


def new_ulid() -> str:
    """Return a fresh ULID string."""
    return ulid_to_str(new_ulid_bytes())
