"""Key-encryption keys (KEK): where they come from (DATA_AND_PRIVACY §6.3).

Two sources (owner decision D3, 2026-11-03: the passphrase is the v0.1 default;
the installer does not program OTP):

1. **Passphrase (the default on every board).** A random 32-byte KEK is
   wrapped by ``argon2id(passphrase, device_salt)`` and stored in
   ``keys/kek.passphrase.wrapped``; the passphrase comes from
   ``BB_KEK_PASSPHRASE_FILE`` at boot. Changing the passphrase re-wraps one
   file; the DEKs in the keyring are untouched.
2. **OTP (optional later upgrade).** ``KEK = HKDF-SHA256(ikm=otp_key,
   salt=device_salt, info="beepboop/kek/v<kek_version>")`` on a Pi whose OTP
   key the owner programmed. Nothing secret is on disk. ``kek_version`` is part
   of the HKDF ``info`` so that KEK rotation (``kek_version++``) yields a new KEK
   from the same, unchangeable OTP key.

:func:`resolve_kek` uses an existing passphrase file first, because that is the
KEK the keyring was wrapped with: programming OTP later (for beepboop or for
anything else, such as a LUKS provisioner) must not strand the data. Moving an
install to OTP is an explicit ``rotate_kek`` followed by deleting the file.

``device_salt`` is 32 random bytes created once at install (``keys/device_salt``)
and never rotated; a reinstalled Pi therefore derives a different KEK.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from argon2.low_level import Type, hash_secret_raw
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import BaseModel, ConfigDict, Field

from beepboop.secrets._ulid import new_ulid_bytes
from beepboop.secrets.envelope import KEY_BYTES, Envelope, open_, row_aad, seal
from beepboop.secrets.otp import OtpUnavailableError

KekKind = Literal["otp", "passphrase"]

DEVICE_SALT_BYTES = 32
DEVICE_SALT_FILE = "device_salt"
PASSPHRASE_KEK_FILE = "kek.passphrase.wrapped"  # noqa: S105 - a file name, not a secret


class KekUnavailableError(RuntimeError):
    """No KEK source works: no OTP key and no passphrase configured."""


@dataclass(frozen=True, slots=True)
class Kek:
    """A KEK in memory. ``key`` is excluded from ``repr`` so it cannot leak into logs."""

    kind: KekKind
    version: int
    key: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if len(self.key) != KEY_BYTES:
            msg = f"a KEK is {KEY_BYTES} bytes"
            raise ValueError(msg)
        if self.version < 1:
            msg = "kek_version starts at 1"
            raise ValueError(msg)

    def fingerprint(self) -> str:
        """A short, non-reversible id for comparing KEKs across runs (V5 probe)."""
        return hashlib.sha256(b"beepboop/kek-fingerprint\x00" + self.key).hexdigest()[:16]


# --------------------------------------------------------------- device salt --


def load_device_salt(keys_dir: Path, *, create: bool = False) -> bytes:
    """Read ``keys_dir/device_salt``; create it (mode 0600) only if ``create`` is set."""
    path = keys_dir / DEVICE_SALT_FILE
    if path.exists():
        salt = path.read_bytes()
        if len(salt) != DEVICE_SALT_BYTES:
            msg = f"{path} must hold {DEVICE_SALT_BYTES} bytes, found {len(salt)}"
            raise ValueError(msg)
        return salt
    if not create:
        msg = f"{path} is missing; it is created once at install"
        raise FileNotFoundError(msg)
    salt = secrets.token_bytes(DEVICE_SALT_BYTES)
    _write_private(path, salt)
    return salt


# ----------------------------------------------------------------------- OTP --


def derive_otp_kek(otp_key: bytes, device_salt: bytes, version: int = 1) -> Kek:
    """HKDF-SHA256 over the OTP key, as drawn in DATA_AND_PRIVACY §6.3."""
    if len(otp_key) != 32 or otp_key == bytes(32):
        msg = "the OTP key must be 32 non-zero bytes"
        raise ValueError(msg)
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_BYTES,
        salt=device_salt,
        info=f"beepboop/kek/v{version}".encode(),
    )
    return Kek(kind="otp", version=version, key=hkdf.derive(otp_key))


# ---------------------------------------------------------------- passphrase --


class Argon2Params(BaseModel):
    """argon2id cost. Defaults are RFC 9106's second recommended option (64 MiB)."""

    model_config = ConfigDict(frozen=True)

    time_cost: int = Field(default=3, ge=1)
    memory_cost_kib: int = Field(default=64 * 1024, ge=8)
    parallelism: int = Field(default=4, ge=1)


class _PassphraseKekFile(BaseModel):
    """On-disk JSON for ``keys/kek.passphrase.wrapped``."""

    format: Literal["beepboop/kek.passphrase.wrapped"] = "beepboop/kek.passphrase.wrapped"
    kdf: Literal["argon2id"] = "argon2id"
    params: Argon2Params
    kek_version: int
    envelope_b64: str


def _passphrase_key(passphrase: str, device_salt: bytes, params: Argon2Params) -> bytes:
    return hash_secret_raw(
        secret=passphrase.encode(),
        salt=device_salt,
        time_cost=params.time_cost,
        memory_cost=params.memory_cost_kib,
        parallelism=params.parallelism,
        hash_len=KEY_BYTES,
        type=Type.ID,
    )


def _kek_file_aad(version: int) -> bytes:
    return row_aad("keys", PASSPHRASE_KEK_FILE, f"kek_v{version}")


def create_passphrase_kek(
    path: Path,
    passphrase: str,
    device_salt: bytes,
    *,
    version: int = 1,
    params: Argon2Params | None = None,
) -> Kek:
    """Generate a random KEK, wrap it under the passphrase and write ``path`` (0600)."""
    if not passphrase:
        msg = "the KEK passphrase must not be empty"
        raise ValueError(msg)
    params = params or Argon2Params()
    kek = Kek(kind="passphrase", version=version, key=secrets.token_bytes(KEY_BYTES))
    wrapping_key = _passphrase_key(passphrase, device_salt, params)
    envelope = seal(wrapping_key, new_ulid_bytes(), kek.key, _kek_file_aad(version))
    record = _PassphraseKekFile(
        params=params,
        kek_version=version,
        envelope_b64=base64.b64encode(envelope.to_bytes()).decode(),
    )
    _write_private(path, record.model_dump_json(indent=2).encode())
    return kek


def load_passphrase_kek(path: Path, passphrase: str, device_salt: bytes) -> Kek:
    """Unwrap the KEK in ``path``; a wrong passphrase raises ``DecryptionError``."""
    record = _PassphraseKekFile.model_validate(json.loads(path.read_text(encoding="utf-8")))
    wrapping_key = _passphrase_key(passphrase, device_salt, record.params)
    envelope = Envelope.from_bytes(base64.b64decode(record.envelope_b64))
    key = open_(wrapping_key, envelope, _kek_file_aad(record.kek_version))
    return Kek(kind="passphrase", version=record.kek_version, key=key)


def read_passphrase_file(path: Path) -> str:
    """Read ``BB_KEK_PASSPHRASE_FILE``: the whole file minus one trailing newline."""
    text = path.read_text(encoding="utf-8")
    return text.removesuffix("\n").removesuffix("\r")


# ------------------------------------------------------------------- resolve --


OtpReader = Callable[[], bytes | None]


def resolve_kek(
    keys_dir: Path,
    *,
    otp_reader: OtpReader,
    passphrase: str | None,
    otp_kek_version: int = 1,
    create_passphrase_file: bool = False,
    params: Argon2Params | None = None,
) -> Kek:
    """Pick the KEK for this board (DATA_AND_PRIVACY §6.3, D3).

    An existing ``keys/kek.passphrase.wrapped`` always wins. Without one, an
    OTP key is used if the owner programmed it, else a passphrase file is
    created (``create_passphrase_file``, the installer's default path).
    ``otp_reader`` returns the OTP key, ``None`` when unprogrammed, or raises
    :class:`beepboop.secrets.otp.OtpUnavailableError` off-Pi.
    ``otp_kek_version`` comes from the keyring (§6.1).
    """
    device_salt = load_device_salt(keys_dir)
    path = keys_dir / PASSPHRASE_KEK_FILE
    if path.exists():
        if passphrase is None:
            msg = f"{path} exists but no BB_KEK_PASSPHRASE_FILE is configured"
            raise KekUnavailableError(msg)
        return load_passphrase_kek(path, passphrase, device_salt)
    try:
        otp_key = otp_reader()
    except OtpUnavailableError:
        otp_key = None
    if otp_key is not None:
        return derive_otp_kek(otp_key, device_salt, otp_kek_version)
    if passphrase is None:
        msg = "no OTP key on this board and no BB_KEK_PASSPHRASE_FILE configured"
        raise KekUnavailableError(msg)
    if not create_passphrase_file:
        msg = f"{path} is missing; create it at install"
        raise KekUnavailableError(msg)
    return create_passphrase_kek(path, passphrase, device_salt, params=params)


def _write_private(path: Path, data: bytes) -> None:
    """Write ``data`` atomically with mode 0600 (best effort on Windows)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)
