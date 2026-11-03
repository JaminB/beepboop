"""The secrets envelope: XChaCha20-Poly1305 with a self-describing header.

DATA_AND_PRIVACY §6.3 owns the format; this module is the one implementation::

    version (1 byte) ‖ key_id (16 bytes) ‖ nonce (24 bytes) ‖ ciphertext ‖ tag (16 bytes)

* ``version`` is :data:`ENVELOPE_VERSION` (1).
* ``key_id`` is the 16-byte ULID of the key that sealed it: the DEK for a data
  row, or the wrapped DEK's own id for a ``keyring`` row (§6.3: "same envelope
  format").
* ``nonce`` is 24 random bytes. XChaCha20's 192-bit nonce makes random nonces
  safe, and ChaCha20 is fast on the Pi 4, which has no AES instructions.
* The AAD binds a ciphertext to its row: ``"<table>|<row id>|<column>"``
  (:func:`row_aad`). Moving a ciphertext to another row fails to decrypt.

Plaintext never leaves this module except as the return value of :func:`open_`.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from nacl import bindings
from nacl.exceptions import CryptoError

ENVELOPE_VERSION = 1
KEY_BYTES = bindings.crypto_aead_xchacha20poly1305_ietf_KEYBYTES  # 32
NONCE_BYTES = bindings.crypto_aead_xchacha20poly1305_ietf_NPUBBYTES  # 24
TAG_BYTES = bindings.crypto_aead_xchacha20poly1305_ietf_ABYTES  # 16
KEY_ID_BYTES = 16
HEADER_BYTES = 1 + KEY_ID_BYTES + NONCE_BYTES


class EnvelopeError(ValueError):
    """The bytes are not a well-formed envelope."""


class DecryptionError(Exception):
    """Authentication failed: wrong key, wrong AAD (moved row) or tampered bytes."""


@dataclass(frozen=True, slots=True)
class Envelope:
    """A parsed envelope. ``sealed`` is ciphertext followed by the 16-byte tag."""

    key_id: bytes
    nonce: bytes
    sealed: bytes
    version: int = ENVELOPE_VERSION

    def to_bytes(self) -> bytes:
        """Serialize to the §6.3 wire format."""
        return bytes([self.version]) + self.key_id + self.nonce + self.sealed

    @classmethod
    def from_bytes(cls, blob: bytes) -> Envelope:
        """Parse the §6.3 wire format; raise :class:`EnvelopeError` if malformed."""
        if len(blob) < HEADER_BYTES + TAG_BYTES:
            msg = f"envelope too short ({len(blob)} bytes)"
            raise EnvelopeError(msg)
        if blob[0] != ENVELOPE_VERSION:
            msg = f"unsupported envelope version {blob[0]}"
            raise EnvelopeError(msg)
        return cls(
            key_id=blob[1 : 1 + KEY_ID_BYTES],
            nonce=blob[1 + KEY_ID_BYTES : HEADER_BYTES],
            sealed=blob[HEADER_BYTES:],
        )


def row_aad(table: str, row_id: str, column: str) -> bytes:
    """The §6.3 associated data, ``"<table>|<row id>|<column>"``."""
    return f"{table}|{row_id}|{column}".encode()


def seal(key: bytes, key_id: bytes, plaintext: bytes, aad: bytes) -> Envelope:
    """Encrypt ``plaintext`` under ``key`` with a fresh random nonce."""
    _check_key(key, key_id)
    nonce = secrets.token_bytes(NONCE_BYTES)
    sealed = bindings.crypto_aead_xchacha20poly1305_ietf_encrypt(plaintext, aad, nonce, key)
    return Envelope(key_id=key_id, nonce=nonce, sealed=sealed)


def open_(key: bytes, envelope: Envelope, aad: bytes) -> bytes:
    """Decrypt and authenticate ``envelope``; raise :class:`DecryptionError` on failure."""
    _check_key(key, envelope.key_id)
    if envelope.version != ENVELOPE_VERSION or len(envelope.nonce) != NONCE_BYTES:
        msg = "malformed envelope"
        raise EnvelopeError(msg)
    try:
        return bindings.crypto_aead_xchacha20poly1305_ietf_decrypt(
            envelope.sealed, aad, envelope.nonce, key
        )
    except CryptoError as exc:
        msg = "envelope failed authentication (wrong key, wrong row, or tampered)"
        raise DecryptionError(msg) from exc


def _check_key(key: bytes, key_id: bytes) -> None:
    if len(key) != KEY_BYTES:
        msg = f"key must be {KEY_BYTES} bytes"
        raise ValueError(msg)
    if len(key_id) != KEY_ID_BYTES:
        msg = f"key id must be {KEY_ID_BYTES} bytes"
        raise ValueError(msg)
