"""Web credentials (docs/WEB_UI.md §6.1): passwords, TOTP, recovery codes, tokens, sealing.

Everything here is pure or nearly so, so it is unit-testable without the app:

* :class:`Passwords`: argon2id ``m=64 MiB, t=3, p=1`` (tests pass cheaper parameters),
  minimum 12 characters, no composition rules, a common-password list.
* TOTP (RFC 6238): SHA-1, 6 digits, 30 s step, ±1 step drift, and a last-used step that
  blocks replay. Implemented on ``hmac`` from the standard library.
* Recovery codes: 10 x 8 characters, stored as SHA-256 hashes, single use.
* :class:`ColumnSealer`: how ``logins.totp_secret_enc`` and ``profiles.pin_hash_enc`` are
  envelope-encrypted under the active DEK (DATA_AND_PRIVACY §6.3).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from urllib.parse import quote

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from beepboop.db.database import Database
from beepboop.secrets import SecretStore
from beepboop.secrets.envelope import Envelope, open_, seal

# ----------------------------------------------------------------- passwords --

PASSWORD_MIN_LEN = 12
#: A short list of common long passwords; the UI's strength meter does the rest.
COMMON_PASSWORDS: frozenset[str] = frozenset(
    {
        "123456789012",
        "1234567890123",
        "12345678901234",
        "password1234",
        "password12345",
        "passwordpassword",
        "qwertyuiopas",
        "qwerty123456",
        "iloveyou1234",
        "letmein12345",
        "beepboop1234",
        "beepbooppassword",
        "aaaaaaaaaaaa",
        "abcdefghijkl",
        "abc123abc123",
        "welcome12345",
        "administrator",
        "changeme1234",
    }
)


class PasswordPolicyError(ValueError):
    """The password is too short or too common. ``code`` is the stable API code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def check_password_policy(password: str, *, username: str | None = None) -> None:
    if len(password) < PASSWORD_MIN_LEN:
        raise PasswordPolicyError("password_too_short")
    lowered = password.lower()
    if lowered in COMMON_PASSWORDS or (username and lowered == username.lower()):
        raise PasswordPolicyError("password_too_common")


@dataclass(frozen=True, slots=True)
class Passwords:
    """argon2id with the WEB_UI §6.1 parameters (memory in KiB)."""

    memory_kib: int = 64 * 1024
    time_cost: int = 3
    parallelism: int = 1

    def _hasher(self) -> PasswordHasher:
        return PasswordHasher(
            time_cost=self.time_cost, memory_cost=self.memory_kib, parallelism=self.parallelism
        )

    def hash(self, password: str) -> str:
        return self._hasher().hash(password)

    def verify(self, encoded: str, password: str) -> bool:
        try:
            return self._hasher().verify(encoded, password)
        except (VerificationError, InvalidHashError):
            return False

    def dummy_verify(self, password: str) -> None:
        """Spend the same time on an unknown username (no user enumeration by timing)."""
        self.verify(_DUMMY_HASHES.setdefault(self, self.hash("dummy-password!")), password)


_DUMMY_HASHES: dict[Passwords, str] = {}

# ---------------------------------------------------------------------- TOTP --

TOTP_STEP_S = 30
TOTP_DIGITS = 6
TOTP_DRIFT_STEPS = 1
TOTP_SECRET_BYTES = 20


def new_totp_secret() -> str:
    """A base32 secret (160 bits), shown as QR + manual key."""
    return base64.b32encode(secrets.token_bytes(TOTP_SECRET_BYTES)).decode().rstrip("=")


def _b32decode(secret: str) -> bytes:
    s = secret.replace(" ", "").replace("-", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def totp_step(when: datetime) -> int:
    return int(when.timestamp()) // TOTP_STEP_S


def totp_code(secret: str, step: int) -> str:
    """RFC 6238 / RFC 4226 HOTP over the step counter, SHA-1, 6 digits."""
    digest = hmac.new(_b32decode(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**TOTP_DIGITS).zfill(TOTP_DIGITS)


def verify_totp(secret: str, code: str, when: datetime, last_step: int | None) -> int | None:
    """Return the matched step (store it as ``totp_last_step``) or ``None``.

    Accepts ±1 step of drift; a step at or before ``last_step`` is a replay and fails.
    """
    code = code.strip().replace(" ", "")
    if len(code) != TOTP_DIGITS or not code.isdigit():
        return None
    now_step = totp_step(when)
    for step in range(now_step - TOTP_DRIFT_STEPS, now_step + TOTP_DRIFT_STEPS + 1):
        if last_step is not None and step <= last_step:
            continue
        if hmac.compare_digest(totp_code(secret, step), code):
            return step
    return None


def provisioning_uri(secret: str, username: str, issuer: str = "beepboop") -> str:
    """``otpauth://`` URI for the authenticator QR code."""
    label = quote(f"{issuer}:{username}")
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}"
        f"&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_STEP_S}"
    )


# ------------------------------------------------------------ recovery codes --

RECOVERY_CODE_COUNT = 10
RECOVERY_CODE_LEN = 8
_RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i


def new_recovery_codes() -> list[str]:
    return [
        "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(RECOVERY_CODE_LEN))
        for _ in range(RECOVERY_CODE_COUNT)
    ]


def hash_recovery_code(code: str) -> str:
    return hashlib.sha256(code.strip().lower().replace("-", "").encode()).hexdigest()


def consume_recovery_code(hashes: list[str], code: str) -> list[str] | None:
    """Return the remaining hashes if ``code`` matched one (single use), else ``None``."""
    h = hash_recovery_code(code)
    for i, existing in enumerate(hashes):
        if hmac.compare_digest(existing, h):
            return hashes[:i] + hashes[i + 1 :]
    return None


# -------------------------------------------------------------------- tokens --


def new_token() -> str:
    """A 256-bit URL-safe token (session cookies, invite links)."""
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def public_id(token_hash_hex: str) -> str:
    """A short, non-secret handle for a hashed token (list and revoke without the token)."""
    return token_hash_hex[:16]


# ------------------------------------------------------------------- sealing --


class SealerUnavailableError(RuntimeError):
    """No KEK is loaded, so nothing can be encrypted at rest (WEB_UI §6.1)."""


class ColumnSealer(Protocol):
    """Envelope-encrypt one database column value (``*_enc`` columns)."""

    async def seal(self, aad: bytes, plaintext: bytes) -> bytes: ...
    async def open(self, aad: bytes, blob: bytes) -> bytes: ...


def column_aad(table: str, row_id: str, column: str) -> bytes:
    """The DATA_AND_PRIVACY §6.3 associated data (binds the blob to its row and column)."""
    return f"{table}|{row_id}|{column}".encode()


@dataclass
class DekColumnSealer:
    """Seal under the active keyring DEK, the same envelope format as ``secrets`` rows.

    Uses only the public ``SecretStore.export_deks`` and the envelope primitives; the DEK
    map is cached for the process like every other unwrapped key.
    """

    db: Database
    store: SecretStore | None
    _deks: dict[str, bytes] | None = field(default=None, repr=False)

    async def _keys(self) -> dict[str, bytes]:
        if self.store is None:
            raise SealerUnavailableError("no KEK")
        if self._deks is None:
            self._deks = await self.store.export_deks()
        return self._deks

    async def seal(self, aad: bytes, plaintext: bytes) -> bytes:
        keys = await self._keys()
        row = await self.db.read(
            lambda c: c.execute(
                "SELECT id FROM keyring WHERE active = 1 ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        )
        if row is None or str(row["id"]) not in keys:
            self._deks = None
            keys = await self._keys()
            if row is None or str(row["id"]) not in keys:
                raise SealerUnavailableError("no active DEK")
        dek_id = str(row["id"])
        return seal(keys[dek_id], _key_id(dek_id), plaintext, aad).to_bytes()

    async def open(self, aad: bytes, blob: bytes) -> bytes:
        keys = await self._keys()
        env = Envelope.from_bytes(blob)
        for dek_id, key in keys.items():
            if _key_id(dek_id) == env.key_id:
                return open_(key, env, aad)
        self._deks = None
        for dek_id, key in (await self._keys()).items():
            if _key_id(dek_id) == env.key_id:
                return open_(key, env, aad)
        raise SealerUnavailableError("unknown DEK")


def _key_id(dek_id: str) -> bytes:
    """The 16-byte envelope key id of a ULID DEK id (Crockford base32 → bytes)."""
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    value = 0
    for ch in dek_id.upper():
        value = value * 32 + alphabet.index(ch)
    return value.to_bytes(16, "big")


__all__ = [
    "COMMON_PASSWORDS",
    "PASSWORD_MIN_LEN",
    "RECOVERY_CODE_COUNT",
    "ColumnSealer",
    "DekColumnSealer",
    "PasswordPolicyError",
    "Passwords",
    "SealerUnavailableError",
    "check_password_policy",
    "column_aad",
    "consume_recovery_code",
    "hash_recovery_code",
    "new_recovery_codes",
    "new_token",
    "new_totp_secret",
    "provisioning_uri",
    "public_id",
    "token_hash",
    "totp_code",
    "totp_step",
    "verify_totp",
]
