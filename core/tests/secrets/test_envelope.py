"""The §6.3 envelope: format, authentication and AAD binding."""

from __future__ import annotations

import secrets

import pytest
from hypothesis import given
from hypothesis import strategies as st

from beepboop.secrets._ulid import new_ulid, new_ulid_bytes, ulid_from_str, ulid_to_str
from beepboop.secrets.envelope import (
    HEADER_BYTES,
    TAG_BYTES,
    DecryptionError,
    Envelope,
    EnvelopeError,
    open_,
    row_aad,
    seal,
)

KEY = bytes(range(32))
KEY_ID = bytes(range(16))
AAD = row_aad("secrets", "01J0000000000000000000000", "ciphertext")


def test_wire_format_layout() -> None:
    blob = seal(KEY, KEY_ID, b"hello", AAD).to_bytes()
    assert blob[0] == 1
    assert blob[1:17] == KEY_ID
    assert len(blob) == HEADER_BYTES + len(b"hello") + TAG_BYTES == 1 + 16 + 24 + 5 + 16


def test_row_aad_format() -> None:
    assert row_aad("keyring", "ID", "wrapped_dek") == b"keyring|ID|wrapped_dek"


@given(st.binary(max_size=4096), st.binary(max_size=64))
def test_round_trip(plaintext: bytes, aad: bytes) -> None:
    envelope = Envelope.from_bytes(seal(KEY, KEY_ID, plaintext, aad).to_bytes())
    assert open_(KEY, envelope, aad) == plaintext


def test_nonces_are_fresh() -> None:
    assert seal(KEY, KEY_ID, b"x", AAD).nonce != seal(KEY, KEY_ID, b"x", AAD).nonce


def test_ciphertext_moved_to_another_row_fails() -> None:
    envelope = seal(KEY, KEY_ID, b"token", AAD)
    with pytest.raises(DecryptionError):
        open_(KEY, envelope, row_aad("secrets", "another-row", "ciphertext"))


def test_wrong_key_fails() -> None:
    envelope = seal(KEY, KEY_ID, b"token", AAD)
    with pytest.raises(DecryptionError):
        open_(secrets.token_bytes(32), envelope, AAD)


def test_tampering_fails() -> None:
    blob = bytearray(seal(KEY, KEY_ID, b"token", AAD).to_bytes())
    blob[-1] ^= 0x01
    with pytest.raises(DecryptionError):
        open_(KEY, Envelope.from_bytes(bytes(blob)), AAD)


@pytest.mark.parametrize("blob", [b"", b"\x01" * 10, b"\x02" + bytes(60)])
def test_malformed_envelopes_are_rejected(blob: bytes) -> None:
    with pytest.raises(EnvelopeError):
        Envelope.from_bytes(blob)


def test_bad_key_sizes_are_rejected() -> None:
    with pytest.raises(ValueError, match="key must be"):
        seal(b"short", KEY_ID, b"x", AAD)
    with pytest.raises(ValueError, match="key id"):
        seal(KEY, b"short", b"x", AAD)


def test_ulid_round_trip_and_order() -> None:
    raw = new_ulid_bytes(now_ms=1_700_000_000_000)
    text = ulid_to_str(raw)
    assert len(text) == 26
    assert ulid_from_str(text) == raw
    assert ulid_from_str(text.lower()) == raw
    assert ulid_to_str(new_ulid_bytes(now_ms=1)) < ulid_to_str(new_ulid_bytes(now_ms=2))
    assert len(new_ulid()) == 26


@pytest.mark.parametrize("bad", ["", "0" * 25, "U" * 26, "8" + "0" * 25])
def test_ulid_rejects_bad_strings(bad: str) -> None:
    with pytest.raises(ValueError, match="ULID"):
        ulid_from_str(bad)
