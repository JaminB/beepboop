"""Spoken-PIN verification inside the audio service (VOICE_PIPELINE §12, IDENTITY_AND_TRUST §8.4).

The digits never leave this process: they are decoded by the zipformer in
``mode="pin"`` (nothing is emitted), mapped to digits here, verified with
argon2id against the ``pin_hash`` core sent in ``capture_pin``, and only the
outcome (``ok | mismatch | timeout | cancelled``) is returned. The digit buffer
is a ``bytearray`` that is zeroed after verification; the decoded text is
dropped immediately (a Python ``str`` cannot be zeroed, so it is never stored
or logged). Nothing in this module logs a value.
"""

from __future__ import annotations

from typing import Literal

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

PinOutcome = Literal["ok", "mismatch", "timeout", "cancelled"]

#: Spoken digit words, including the common homophones the recogniser produces.
DIGIT_WORDS: dict[str, int] = {
    "zero": 0,
    "oh": 0,
    "o": 0,
    "nought": 0,
    "one": 1,
    "won": 1,
    "two": 2,
    "to": 2,
    "too": 2,
    "three": 3,
    "four": 4,
    "for": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "ate": 8,
    "nine": 9,
}

_hasher = PasswordHasher()


def words_to_digits(text: str) -> bytearray | None:
    """``"four seven one nine"`` → ``bytearray(b"4719")``; None if any word is not a digit."""
    out = bytearray()
    for raw in text.lower().replace("-", " ").split():
        word = raw.strip(".,?!")
        if not word:
            continue
        if word.isdigit():
            out.extend(word.encode("ascii"))
            continue
        digit = DIGIT_WORDS.get(word)
        if digit is None:
            zero(out)
            return None
        out.append(ord("0") + digit)
    return out


def zero(buf: bytearray) -> None:
    for i in range(len(buf)):
        buf[i] = 0


def verify(pin_hash: str, digits: bytearray, *, min_len: int, max_len: int) -> PinOutcome:
    """argon2id verification; the buffer is zeroed before returning, whatever happens."""
    try:
        if not min_len <= len(digits) <= max_len:
            return "mismatch"
        try:
            ok = _hasher.verify(pin_hash, bytes(digits))
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return "mismatch"
        return "ok" if ok else "mismatch"
    finally:
        zero(digits)


def outcome_for_text(text: str, pin_hash: str, *, min_len: int, max_len: int) -> PinOutcome:
    """The whole decision for one decoded utterance (empty text means nothing was said)."""
    if not text.strip():
        return "timeout"
    digits = words_to_digits(text)
    if digits is None:
        return "mismatch"
    return verify(pin_hash, digits, min_len=min_len, max_len=max_len)


__all__ = ["DIGIT_WORDS", "PinOutcome", "outcome_for_text", "verify", "words_to_digits", "zero"]
