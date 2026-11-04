"""Envelope-encrypted secrets in SQLite: the ``secrets`` and ``keyring`` tables.

DATA_AND_PRIVACY §6.1 owns the DDL and §6.3 the crypto. In short:

* ``keyring`` holds DEKs (32 random bytes each), every one wrapped by the KEK
  in the §6.3 envelope (``key_id`` = the DEK's own id, AAD
  ``keyring|<id>|wrapped_dek``), tagged with ``kek_kind`` and ``kek_version``.
  Exactly one DEK is ``active``; new ciphertexts use it.
* ``secrets`` rows are sealed under a DEK with AAD ``secrets|<id>|ciphertext``.
  As §6.1 specifies, the nonce lives in its own column and ``ciphertext`` holds
  ciphertext ‖ tag; ``dek_id`` names the DEK.

Two ways to open a store:

* :meth:`SecretStore.attach` (core, v0.1): the tables are the ones ``0001_init.sql``
  created; writes go through the single-writer task as durable jobs
  (``synchronous=FULL``, DATA_AND_PRIVACY §3, §4) and reads use the read pool.
* :meth:`SecretStore.open` (probes, standalone tools): its own connection on a
  file, creating the two tables if absent (without the ``profiles`` FK).

Rotation (``beepboop keys rotate-dek``, §6.3): :meth:`SecretStore.rotate_dek` and
:meth:`SecretStore.rotate_kek`. Backups (§11): :func:`export_deks` and
:func:`import_deks`. Plaintext is returned as :class:`pydantic.SecretStr` and
never cached.
"""

from __future__ import annotations

import asyncio
import secrets
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Protocol, Self

from pydantic import SecretStr
from structlog.typing import FilteringBoundLogger

from beepboop.log import get_logger
from beepboop.secrets._ulid import new_ulid, new_ulid_bytes, ulid_from_str, ulid_to_str
from beepboop.secrets.envelope import KEY_BYTES, Envelope, open_, row_aad, seal
from beepboop.secrets.kek import Kek

if TYPE_CHECKING:
    from beepboop.db.database import Database


def _log() -> FilteringBoundLogger:
    """The module logger, looked up at call time (``beepboop.log.get_logger`` is lazy too)."""
    return get_logger(__name__)


#: Subset of DATA_AND_PRIVACY §6.1 for standalone files (:meth:`SecretStore.open`).
#: ``profiles`` does not exist there, so the ``profile_id`` foreign key is omitted;
#: inside the core database ``0001_init.sql`` is the only DDL.
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


# ------------------------------------------------------------------ backends --


class _Backend(Protocol):
    """Where the SQL runs. ``write`` runs ``fn`` inside one transaction."""

    async def read[T](self, fn: Callable[[sqlite3.Connection], T]) -> T: ...
    async def write[T](self, fn: Callable[[sqlite3.Connection], T]) -> T: ...
    async def close(self) -> None: ...


class _OwnConnection:
    """Standalone file: one connection, one job at a time, in a worker thread."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = asyncio.Lock()

    async def read[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        async with self._lock:
            return await asyncio.to_thread(fn, self._conn)

    async def write[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        def _tx(conn: sqlite3.Connection) -> T:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(conn)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            return result

        async with self._lock:
            return await asyncio.to_thread(_tx, self._conn)

    async def close(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._conn.close)


class _DatabaseBackend:
    """The core database: durable writer jobs and the read pool (DATA_AND_PRIVACY §4)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def read[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        return await self._db.read(fn)

    async def write[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        return await self._db.write(fn, durable=True)

    async def close(self) -> None:
        """The database belongs to the service; closing the store leaves it open."""


# --------------------------------------------------------------------- store --


class SecretStore:
    """Async facade over the two tables. Build with :meth:`attach` or :meth:`open`."""

    def __init__(self, backend: _Backend, kek: Kek) -> None:
        self._backend = backend
        self._kek = kek

    # ---------------------------------------------------------- lifecycle --

    @classmethod
    async def attach(cls, db: Database, kek: Kek) -> SecretStore:
        """Use the core database's ``keyring``/``secrets`` tables (``0001_init.sql``)."""
        store = cls(_DatabaseBackend(db), kek)
        await store._ensure_keyring()
        return store

    @classmethod
    async def open(cls, path: Path, kek: Kek) -> SecretStore:
        """Open (or create) a standalone file at ``path`` and ensure an active DEK exists."""

        def _connect() -> sqlite3.Connection:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")  # §3: secrets and keyring are FULL
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(SCHEMA)
            return conn

        store = cls(_OwnConnection(await asyncio.to_thread(_connect)), kek)
        await store._ensure_keyring()
        return store

    async def close(self) -> None:
        """Release the backend (a no-op for :meth:`attach`)."""
        await self._backend.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    @property
    def kek(self) -> Kek:
        """The KEK currently wrapping the keyring."""
        return self._kek

    # ------------------------------------------------------------- public --

    async def put(self, ref: SecretRef, value: SecretStr | str) -> None:
        """Create or replace a secret."""
        plaintext = value.get_secret_value() if isinstance(value, SecretStr) else value
        kek = self._kek
        await self._backend.write(lambda c: _put(c, kek, ref, plaintext.encode()))
        _log().info("secrets.put", name=ref.name, owner_module=ref.owner_module)

    async def get(self, ref: SecretRef) -> SecretStr:
        """Decrypt a secret; raise :class:`SecretNotFoundError` if absent."""
        kek = self._kek
        return SecretStr((await self._backend.read(lambda c: _get(c, kek, ref))).decode())

    async def exists(self, ref: SecretRef) -> bool:
        """True if the secret is stored (no decryption)."""
        return await self._backend.read(lambda c: _find(c, ref) is not None)

    async def delete(self, ref: SecretRef) -> bool:
        """Delete a secret; return whether it existed."""
        return await self._backend.write(lambda c: _delete(c, ref))

    async def rotate_dek(self) -> str:
        """Create a new active DEK and re-seal every row under it. Returns the new DEK id."""
        kek = self._kek
        dek_id = await self._backend.write(lambda c: _rotate_dek(c, kek))
        _log().info("secrets.dek_rotated", dek_id=dek_id)
        return dek_id

    async def rotate_kek(self, new_kek: Kek) -> None:
        """Re-wrap every DEK under ``new_kek`` (normally ``kek_version + 1``)."""
        old = self._kek
        await self._backend.write(lambda c: _rewrap_all(c, old, new_kek))
        self._kek = new_kek
        _log().info("secrets.kek_rotated", kek_kind=new_kek.kind, kek_version=new_kek.version)

    async def export_deks(self) -> dict[str, bytes]:
        """Every keyring DEK in plaintext, for one backup bundle only (§11)."""
        kek = self._kek
        return await self._backend.read(lambda c: export_deks(c, kek))

    async def _ensure_keyring(self) -> None:
        kek = self._kek
        await self._backend.write(lambda c: _ensure_keyring(c, kek))


# ------------------------------------------------------------ sync helpers --
# Plain functions over a connection, so they compose inside one writer job.


def keyring_empty(conn: sqlite3.Connection) -> bool:
    """True before the first DEK exists (first boot, or a fresh database)."""
    return conn.execute("SELECT 1 FROM keyring LIMIT 1").fetchone() is None


def _ensure_keyring(conn: sqlite3.Connection, kek: Kek) -> None:
    rows = conn.execute("SELECT kek_kind, kek_version FROM keyring").fetchall()
    for kind, version in rows:
        if (kind, version) != (kek.kind, kek.version):
            msg = (
                f"keyring is wrapped by kek {kind}/v{version}, "
                f"but {kek.kind}/v{kek.version} was supplied"
            )
            raise KekMismatchError(msg)
    if not rows:
        _insert_dek(conn, kek, active=True)


def _wrap(kek: Kek, dek_id: str, dek: bytes) -> bytes:
    aad = row_aad("keyring", dek_id, "wrapped_dek")
    return seal(kek.key, ulid_from_str(dek_id), dek, aad).to_bytes()


def _insert_dek(conn: sqlite3.Connection, kek: Kek, *, active: bool) -> str:
    dek_id = ulid_to_str(new_ulid_bytes())
    conn.execute(
        "INSERT INTO keyring (id, wrapped_dek, kek_kind, kek_version, active, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (dek_id, _wrap(kek, dek_id, _random_key()), kek.kind, kek.version, int(active), _now_ms()),
    )
    return dek_id


def _unwrap_dek(conn: sqlite3.Connection, kek: Kek, dek_id: str) -> bytes:
    row = conn.execute("SELECT wrapped_dek FROM keyring WHERE id = ?", (dek_id,)).fetchone()
    if row is None:
        msg = f"keyring has no DEK {dek_id}"
        raise LookupError(msg)
    envelope = Envelope.from_bytes(row[0])
    if envelope.key_id != ulid_from_str(dek_id):
        msg = f"keyring row {dek_id} holds the wrong DEK"
        raise LookupError(msg)
    return open_(kek.key, envelope, row_aad("keyring", dek_id, "wrapped_dek"))


def _active_dek(conn: sqlite3.Connection, kek: Kek) -> tuple[str, bytes]:
    row = conn.execute("SELECT id FROM keyring WHERE active = 1").fetchone()
    if row is None:
        msg = "keyring has no active DEK"
        raise LookupError(msg)
    dek_id: str = row[0]
    return dek_id, _unwrap_dek(conn, kek, dek_id)


def _find(conn: sqlite3.Connection, ref: SecretRef) -> tuple[str, bytes, bytes, str] | None:
    row = conn.execute(
        "SELECT id, ciphertext, nonce, dek_id FROM secrets"
        " WHERE owner_module = ? AND COALESCE(profile_id, '') = ? AND name = ?",
        (ref.owner_module, ref.profile_id or "", ref.name),
    ).fetchone()
    return None if row is None else (row[0], row[1], row[2], row[3])


def _seal_row(row_id: str, plaintext: bytes, dek_id: str, dek: bytes) -> tuple[bytes, bytes]:
    aad = row_aad("secrets", row_id, "ciphertext")
    envelope = seal(dek, ulid_from_str(dek_id), plaintext, aad)
    return envelope.sealed, envelope.nonce


def _open_row(
    conn: sqlite3.Connection, kek: Kek, row_id: str, sealed: bytes, nonce: bytes, dek_id: str
) -> bytes:
    envelope = Envelope(key_id=ulid_from_str(dek_id), nonce=nonce, sealed=sealed)
    dek = _unwrap_dek(conn, kek, dek_id)
    return open_(dek, envelope, row_aad("secrets", row_id, "ciphertext"))


def _put(conn: sqlite3.Connection, kek: Kek, ref: SecretRef, plaintext: bytes) -> None:
    existing = _find(conn, ref)
    row_id = existing[0] if existing else new_ulid()
    dek_id, dek = _active_dek(conn, kek)
    sealed, nonce = _seal_row(row_id, plaintext, dek_id, dek)
    if existing:
        conn.execute(
            "UPDATE secrets SET ciphertext = ?, nonce = ?, dek_id = ?, updated_at = ? WHERE id = ?",
            (sealed, nonce, dek_id, _now_ms(), row_id),
        )
    else:
        conn.execute(
            "INSERT INTO secrets (id, owner_module, profile_id, name, ciphertext,"
            " nonce, dek_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (row_id, ref.owner_module, ref.profile_id, ref.name, sealed, nonce, dek_id, _now_ms()),
        )


def _get(conn: sqlite3.Connection, kek: Kek, ref: SecretRef) -> bytes:
    found = _find(conn, ref)
    if found is None:
        raise SecretNotFoundError(ref.name)
    return _open_row(conn, kek, *found)


def _delete(conn: sqlite3.Connection, ref: SecretRef) -> bool:
    found = _find(conn, ref)
    if found is not None:
        conn.execute("DELETE FROM secrets WHERE id = ?", (found[0],))
    return found is not None


def _rotate_dek(conn: sqlite3.Connection, kek: Kek) -> str:
    conn.execute("UPDATE keyring SET active = 0")
    new_id = _insert_dek(conn, kek, active=True)
    new_dek = _unwrap_dek(conn, kek, new_id)
    rows = conn.execute("SELECT id, ciphertext, nonce, dek_id FROM secrets").fetchall()
    for row_id, sealed, nonce, dek_id in rows:
        plaintext = _open_row(conn, kek, row_id, sealed, nonce, dek_id)
        new_sealed, new_nonce = _seal_row(row_id, plaintext, new_id, new_dek)
        conn.execute(
            "UPDATE secrets SET ciphertext = ?, nonce = ?, dek_id = ?, updated_at = ? WHERE id = ?",
            (new_sealed, new_nonce, new_id, _now_ms(), row_id),
        )
    return new_id


def _rewrap_all(conn: sqlite3.Connection, old: Kek, new: Kek) -> None:
    import_deks(conn, export_deks(conn, old), new)


def export_deks(conn: sqlite3.Connection, kek: Kek) -> dict[str, bytes]:
    """Unwrap every keyring DEK (``dek.json`` in a backup bundle, DATA_AND_PRIVACY §11)."""
    ids = [row[0] for row in conn.execute("SELECT id FROM keyring ORDER BY id").fetchall()]
    return {dek_id: _unwrap_dek(conn, kek, dek_id) for dek_id in ids}


def import_deks(conn: sqlite3.Connection, deks: Mapping[str, bytes], kek: Kek) -> int:
    """Re-wrap restored DEKs under this Pi's ``kek`` (restore, §11). Returns rows updated.

    Every keyring row must be in ``deks``; a missing one would strand its ciphertexts.
    Run inside one transaction (a durable writer job).
    """
    ids: list[str] = [row[0] for row in conn.execute("SELECT id FROM keyring").fetchall()]
    missing = sorted(set(ids) - set(deks))
    if missing:
        msg = f"backup has no DEK for keyring rows {missing}"
        raise LookupError(msg)
    for dek_id in ids:
        dek = deks[dek_id]
        if len(dek) != KEY_BYTES:
            msg = f"DEK {dek_id} is {len(dek)} bytes, expected {KEY_BYTES}"
            raise ValueError(msg)
        conn.execute(
            "UPDATE keyring SET wrapped_dek = ?, kek_kind = ?, kek_version = ? WHERE id = ?",
            (_wrap(kek, dek_id, dek), kek.kind, kek.version, dek_id),
        )
    return len(ids)


def _random_key() -> bytes:
    return secrets.token_bytes(KEY_BYTES)
