"""Envelope-encrypted secrets in SQLite: the ``secrets`` and ``keyring`` tables.

DATA_AND_PRIVACY §6.1 owns the DDL and §6.3 the crypto. In short:

* ``keyring`` holds DEKs (32 random bytes each), every one wrapped by the KEK
  in the §6.3 envelope (``key_id`` = the DEK's own id, AAD
  ``keyring|<id>|wrapped_dek``), tagged with ``kek_kind`` and ``kek_version``.
  Exactly one DEK is ``active``; new ciphertexts use it.
* ``secrets`` rows are sealed under a DEK with AAD ``secrets|<id>|ciphertext``.
  As §6.1 specifies, the nonce lives in its own column and ``ciphertext`` holds
  ciphertext ‖ tag; ``dek_id`` names the DEK.

Rotation hooks (``beepboop keys rotate-dek``, §6.3):

* :meth:`SecretStore.rotate_dek`: new active DEK, every row re-sealed under it.
* :meth:`SecretStore.rotate_kek`: every DEK re-wrapped under a new KEK
  (``kek_version++``); row ciphertexts are untouched.

**M0 stand-in.** v0.1 moves these tables into the main database behind the
single-writer task (``core/beepboop/db/``, §4). Until then this class owns its
own SQLite file and runs each statement in a worker thread. Plaintext is
returned as :class:`pydantic.SecretStr` and never cached.
"""

from __future__ import annotations

import asyncio
import secrets
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

from pydantic import SecretStr
from structlog.typing import FilteringBoundLogger

from beepboop.log import get_logger
from beepboop.secrets._ulid import new_ulid, new_ulid_bytes, ulid_from_str, ulid_to_str
from beepboop.secrets.envelope import KEY_BYTES, Envelope, open_, row_aad, seal
from beepboop.secrets.kek import Kek


def _log() -> FilteringBoundLogger:
    """The module logger, looked up at call time (``beepboop.log.get_logger`` is lazy too)."""
    return get_logger(__name__)


#: Subset of DATA_AND_PRIVACY §6.1. ``profiles`` does not exist at M0, so the
#: ``profile_id`` foreign key is omitted here; the v0.1 migration adds it.
SCHEMA = """
CREATE TABLE IF NOT EXISTS keyring (
  id            TEXT PRIMARY KEY,
  wrapped_dek   BLOB NOT NULL,
  kek_kind      TEXT NOT NULL CHECK (kek_kind IN ('otp','passphrase')),
  kek_version   INTEGER NOT NULL,
  active        INTEGER NOT NULL DEFAULT 1,
  created_at    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS secrets (
  id            TEXT PRIMARY KEY,
  owner_module  TEXT NOT NULL,
  profile_id    TEXT,
  name          TEXT NOT NULL,
  ciphertext    BLOB NOT NULL,
  nonce         BLOB NOT NULL,
  dek_id        TEXT NOT NULL REFERENCES keyring(id),
  updated_at    INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS secrets_unique
  ON secrets(owner_module, COALESCE(profile_id, ''), name);
"""


class SecretNotFoundError(KeyError):
    """No secret with that (owner_module, profile_id, name)."""


class KekMismatchError(RuntimeError):
    """The keyring was wrapped by a different KEK kind/version than the one supplied."""


@dataclass(frozen=True, slots=True)
class SecretRef:
    """Identifies one secret: ``owner_module`` is a module id or ``"core"``."""

    name: str
    owner_module: str = "core"
    profile_id: str | None = None


def _now_ms() -> int:
    return int(time.time() * 1000)


class SecretStore:
    """Async facade over the two tables. Open with :meth:`open`; close when done."""

    def __init__(self, conn: sqlite3.Connection, kek: Kek) -> None:
        self._conn = conn
        self._kek = kek
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------- lifecycle --

    @classmethod
    async def open(cls, path: Path, kek: Kek) -> SecretStore:
        """Open (or create) the database at ``path`` and ensure an active DEK exists."""

        def _connect() -> sqlite3.Connection:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")  # §3: secrets and keyring are FULL
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(SCHEMA)
            return conn

        store = cls(await asyncio.to_thread(_connect), kek)
        await store._run(store._ensure_keyring)
        return store

    async def close(self) -> None:
        """Close the database connection."""
        async with self._lock:
            await asyncio.to_thread(self._conn.close)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    # ------------------------------------------------------------- public --

    async def put(self, ref: SecretRef, value: SecretStr | str) -> None:
        """Create or replace a secret."""
        plaintext = value.get_secret_value() if isinstance(value, SecretStr) else value
        await self._run(self._put, ref, plaintext.encode())
        _log().info("secrets.put", name=ref.name, owner_module=ref.owner_module)

    async def get(self, ref: SecretRef) -> SecretStr:
        """Decrypt a secret; raise :class:`SecretNotFoundError` if absent."""
        return SecretStr((await self._run(self._get, ref)).decode())

    async def exists(self, ref: SecretRef) -> bool:
        """True if the secret is stored (no decryption)."""
        return await self._run(self._exists, ref)

    async def delete(self, ref: SecretRef) -> bool:
        """Delete a secret; return whether it existed."""
        return await self._run(self._delete, ref)

    async def rotate_dek(self) -> str:
        """Create a new active DEK and re-seal every row under it. Returns the new DEK id."""
        dek_id = await self._run(self._rotate_dek)
        _log().info("secrets.dek_rotated", dek_id=dek_id)
        return dek_id

    async def rotate_kek(self, new_kek: Kek) -> None:
        """Re-wrap every DEK under ``new_kek`` (normally ``kek_version + 1``)."""
        await self._run(self._rotate_kek, new_kek)
        _log().info("secrets.kek_rotated", kek_kind=new_kek.kind, kek_version=new_kek.version)

    # ------------------------------------------------------ sync helpers --
    # Everything below runs in a worker thread, one call at a time (self._lock).

    async def _run[**P, R](self, func: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> R:
        async with self._lock:
            return await asyncio.to_thread(func, *args, **kwargs)

    def _ensure_keyring(self) -> None:
        rows = self._conn.execute("SELECT kek_kind, kek_version FROM keyring").fetchall()
        for kind, version in rows:
            if (kind, version) != (self._kek.kind, self._kek.version):
                msg = (
                    f"keyring is wrapped by kek {kind}/v{version}, "
                    f"but {self._kek.kind}/v{self._kek.version} was supplied"
                )
                raise KekMismatchError(msg)
        if not rows:
            with self._transaction():
                self._insert_dek(active=True)

    def _insert_dek(self, *, active: bool) -> str:
        raw_id = new_ulid_bytes()
        dek_id = ulid_to_str(raw_id)
        dek = _random_key()
        wrapped = seal(self._kek.key, raw_id, dek, row_aad("keyring", dek_id, "wrapped_dek"))
        self._conn.execute(
            "INSERT INTO keyring (id, wrapped_dek, kek_kind, kek_version, active, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (dek_id, wrapped.to_bytes(), self._kek.kind, self._kek.version, int(active), _now_ms()),
        )
        return dek_id

    def _unwrap_dek(self, dek_id: str, kek: Kek | None = None) -> bytes:
        row = self._conn.execute(
            "SELECT wrapped_dek FROM keyring WHERE id = ?", (dek_id,)
        ).fetchone()
        if row is None:
            msg = f"keyring has no DEK {dek_id}"
            raise LookupError(msg)
        envelope = Envelope.from_bytes(row[0])
        if envelope.key_id != ulid_from_str(dek_id):
            msg = f"keyring row {dek_id} holds the wrong DEK"
            raise LookupError(msg)
        key = (kek or self._kek).key
        return open_(key, envelope, row_aad("keyring", dek_id, "wrapped_dek"))

    def _active_dek(self) -> tuple[str, bytes]:
        row = self._conn.execute("SELECT id FROM keyring WHERE active = 1").fetchone()
        if row is None:
            msg = "keyring has no active DEK"
            raise LookupError(msg)
        dek_id: str = row[0]
        return dek_id, self._unwrap_dek(dek_id)

    def _find(self, ref: SecretRef) -> tuple[str, bytes, bytes, str] | None:
        row = self._conn.execute(
            "SELECT id, ciphertext, nonce, dek_id FROM secrets"
            " WHERE owner_module = ? AND COALESCE(profile_id, '') = ? AND name = ?",
            (ref.owner_module, ref.profile_id or "", ref.name),
        ).fetchone()
        return None if row is None else (row[0], row[1], row[2], row[3])

    def _seal_row(self, row_id: str, plaintext: bytes) -> tuple[bytes, bytes, str]:
        dek_id, dek = self._active_dek()
        envelope = seal(
            dek, ulid_from_str(dek_id), plaintext, row_aad("secrets", row_id, "ciphertext")
        )
        return envelope.sealed, envelope.nonce, dek_id

    def _open_row(self, row_id: str, sealed: bytes, nonce: bytes, dek_id: str) -> bytes:
        envelope = Envelope(key_id=ulid_from_str(dek_id), nonce=nonce, sealed=sealed)
        return open_(self._unwrap_dek(dek_id), envelope, row_aad("secrets", row_id, "ciphertext"))

    def _put(self, ref: SecretRef, plaintext: bytes) -> None:
        with self._transaction():
            existing = self._find(ref)
            row_id = existing[0] if existing else new_ulid()
            sealed, nonce, dek_id = self._seal_row(row_id, plaintext)
            if existing:
                self._conn.execute(
                    "UPDATE secrets SET ciphertext = ?, nonce = ?, dek_id = ?, updated_at = ?"
                    " WHERE id = ?",
                    (sealed, nonce, dek_id, _now_ms(), row_id),
                )
            else:
                self._conn.execute(
                    "INSERT INTO secrets (id, owner_module, profile_id, name, ciphertext,"
                    " nonce, dek_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row_id,
                        ref.owner_module,
                        ref.profile_id,
                        ref.name,
                        sealed,
                        nonce,
                        dek_id,
                        _now_ms(),
                    ),
                )

    def _get(self, ref: SecretRef) -> bytes:
        found = self._find(ref)
        if found is None:
            raise SecretNotFoundError(ref.name)
        return self._open_row(*found)

    def _exists(self, ref: SecretRef) -> bool:
        return self._find(ref) is not None

    def _delete(self, ref: SecretRef) -> bool:
        with self._transaction():
            found = self._find(ref)
            if found is not None:
                self._conn.execute("DELETE FROM secrets WHERE id = ?", (found[0],))
            return found is not None

    def _rotate_dek(self) -> str:
        with self._transaction():
            self._conn.execute("UPDATE keyring SET active = 0")
            new_id = self._insert_dek(active=True)
            rows = self._conn.execute(
                "SELECT id, ciphertext, nonce, dek_id FROM secrets"
            ).fetchall()
            for row_id, sealed, nonce, dek_id in rows:
                plaintext = self._open_row(row_id, sealed, nonce, dek_id)
                new_sealed, new_nonce, _ = self._seal_row(row_id, plaintext)
                self._conn.execute(
                    "UPDATE secrets SET ciphertext = ?, nonce = ?, dek_id = ?, updated_at = ?"
                    " WHERE id = ?",
                    (new_sealed, new_nonce, new_id, _now_ms(), row_id),
                )
            return new_id

    def _rotate_kek(self, new_kek: Kek) -> None:
        with self._transaction():
            ids = [row[0] for row in self._conn.execute("SELECT id FROM keyring").fetchall()]
            for dek_id in ids:
                dek = self._unwrap_dek(dek_id)
                wrapped = seal(
                    new_kek.key,
                    ulid_from_str(dek_id),
                    dek,
                    row_aad("keyring", dek_id, "wrapped_dek"),
                )
                self._conn.execute(
                    "UPDATE keyring SET wrapped_dek = ?, kek_kind = ?, kek_version = ?"
                    " WHERE id = ?",
                    (wrapped.to_bytes(), new_kek.kind, new_kek.version, dek_id),
                )
        self._kek = new_kek

    def _transaction(self) -> _Transaction:
        return _Transaction(self._conn)


class _Transaction:
    """``BEGIN IMMEDIATE`` … ``COMMIT``, rolled back on any exception."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __enter__(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._conn.execute("ROLLBACK" if exc_type else "COMMIT")


def _random_key() -> bytes:
    return secrets.token_bytes(KEY_BYTES)
