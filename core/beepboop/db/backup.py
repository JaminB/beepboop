"""Backup bundles, the recovery key and restore (DATA_AND_PRIVACY §11 owns the procedure).

::

    1. writer: PRAGMA wal_checkpoint(TRUNCATE)
    2. writer: VACUUM INTO backups/tmp/beepboop.db
    3. tar in memory: beepboop.db, keys/device_salt, dek.json, ha/, manifest.json
    4. age-encrypt to the recovery recipient(s) (+ the Pi-held test recipient)
    5. copy to each destination; verify size + sha256
    6. prune: 7 daily + 4 weekly per destination
    7. audit backup.ok | backup.fail; household ``backup.last``

``dek.json`` holds every keyring DEK in plaintext and only ever exists inside the
in-memory tar and the encrypted bundle: never on disk, never in a pre-update
snapshot (``beepboop db backup --out``, steps 1-2 only). Restore
(:func:`restore_bundle`) runs with core's database closed (the host stops core
first), re-wraps the DEKs under this Pi's KEK and keeps this Pi's ``device_salt``.

The recovery key is an ``age`` X25519 identity (``AGE-SECRET-KEY-1…``): its text is
the *recovery phrase* the owner types at restore. Only the public recipient is
stored (household ``backup.recipients``).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import tarfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, cast

import pyrage
from pydantic import BaseModel, ConfigDict, SecretStr

from beepboop.db import repo
from beepboop.db.connect import connect, user_version
from beepboop.db.database import Database
from beepboop.db.migrate import MIGRATIONS_DIR, latest_version, migrate
from beepboop.log import get_logger
from beepboop.secrets.kek import Kek
from beepboop.secrets.store import SecretRef, SecretStore, import_deks

BUNDLE_FORMAT = 1
KEEP_DAILY = 7
KEEP_WEEKLY = 4
DEVICE_SALT = "keys/device_salt"
#: The Pi-held test identity for the weekly restore check, envelope-encrypted like any
#: other secret (so a copied data root without the KEK passphrase cannot open bundles).
TEST_IDENTITY = SecretRef("backup_test_identity")
#: household keys this module owns.
RECIPIENTS_KEY = "backup.recipients"
DESTINATIONS_KEY = "backup.destinations"
LAST_KEY = "backup.last"
LAST_CHECK_KEY = "backup.last_restore_check"

_BUNDLE_NAME = re.compile(r"^beepboop-(?P<host>[A-Za-z0-9.-]+)-(?P<ts>\d{8}T\d{6}Z)\.tar\.age$")
#: Files from HA's config dir that go into a bundle; recorder history is disposable.
_HA_FILES = ("configuration.yaml",)
_HA_DIRS = (".storage",)


class BackupError(RuntimeError):
    """A bundle could not be built, verified or restored. The message is user-safe."""


class SchemaTooNewError(BackupError):
    """The bundle's schema ``user_version`` is newer than this code can migrate."""


# ----------------------------------------------------------------- recovery --


@dataclass(frozen=True, slots=True)
class RecoveryKey:
    """A fresh recovery key. Show ``identity`` once (Recovery kit); store ``recipient``."""

    identity: SecretStr
    recipient: str


def generate_recovery_key() -> RecoveryKey:
    """New ``age`` X25519 identity (DATA_AND_PRIVACY §11)."""
    ident = pyrage.x25519.Identity.generate()
    return RecoveryKey(identity=SecretStr(str(ident)), recipient=str(ident.to_public()))


def validate_recipient(recipient: str) -> str:
    """Return ``recipient`` if it parses as an ``age1…`` X25519 recipient."""
    try:
        pyrage.x25519.Recipient.from_str(recipient)
    except Exception as exc:
        msg = "not an age X25519 recipient (age1...)"
        raise BackupError(msg) from exc
    return recipient


def set_recipients(conn: sqlite3.Connection, recipients: Sequence[str]) -> None:
    """Store the recovery recipient(s) (onboarding and the Backups page)."""
    repo.household.set_(conn, RECIPIENTS_KEY, [validate_recipient(r) for r in recipients])


def get_recipients(conn: sqlite3.Connection) -> list[str]:
    value = repo.household.get(conn, RECIPIENTS_KEY)
    return [str(v) for v in cast(list[object], value)] if isinstance(value, list) else []


def get_destinations(conn: sqlite3.Connection) -> list[Path]:
    """Extra destinations (a USB mount path or a mounted SMB share)."""
    value = repo.household.get(conn, DESTINATIONS_KEY)
    return [Path(str(v)) for v in cast(list[object], value)] if isinstance(value, list) else []


async def ensure_test_identity(store: SecretStore) -> str:
    """Create the Pi-held test identity if absent; return its public recipient."""
    if await store.exists(TEST_IDENTITY):
        ident = pyrage.x25519.Identity.from_str((await store.get(TEST_IDENTITY)).get_secret_value())
    else:
        ident = pyrage.x25519.Identity.generate()
        await store.put(TEST_IDENTITY, SecretStr(str(ident)))
    return str(ident.to_public())


# ---------------------------------------------------------------- manifest --


class BundleManifest(BaseModel):
    """``manifest.json`` inside a bundle."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    format: int = BUNDLE_FORMAT
    user_version: int
    core_version: str
    ha_version: str | None
    created_at: datetime
    host: str
    files: dict[str, str]  # name -> sha256 hex


@dataclass(frozen=True, slots=True)
class BackupSources:
    """What a bundle is built from (paths are as core sees them in its container)."""

    db: Database
    secrets: SecretStore
    keys_dir: Path
    backups_dir: Path
    host: str
    core_version: str
    ha_dir: Path | None = None
    ha_version: str | None = None


@dataclass(frozen=True, slots=True)
class DestinationResult:
    destination: str
    ok: bool
    path: str | None = None
    error: str | None = None
    pruned: int = 0


@dataclass(frozen=True, slots=True)
class BackupResult:
    ok: bool
    name: str
    sha256: str
    size: int
    destinations: list[DestinationResult] = field(default_factory=list[DestinationResult])

    def as_dict(self, at: datetime) -> dict[str, object]:
        return {
            "at": at.isoformat(),
            "ok": self.ok,
            "name": self.name,
            "size": self.size,
            "destinations": [
                {"destination": d.destination, "ok": d.ok, "error": d.error}
                for d in self.destinations
            ],
            # "Backups are only on this disk" until a second destination exists (§11).
            "local_only": len(self.destinations) <= 1,
        }


def bundle_name(host: str, now: datetime) -> str:
    return f"beepboop-{host}-{now.astimezone(UTC):%Y%m%dT%H%M%SZ}.tar.age"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _add(tar: tarfile.TarFile, name: str, data: bytes, mtime: int) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = mtime
    info.mode = 0o600
    tar.addfile(info, io.BytesIO(data))


def _ha_members(ha_dir: Path | None) -> dict[str, bytes]:
    if ha_dir is None or not ha_dir.is_dir():
        return {}
    out: dict[str, bytes] = {}
    for name in _HA_FILES:
        p = ha_dir / name
        if p.is_file():
            out[f"ha/{name}"] = p.read_bytes()
    for d in _HA_DIRS:
        root = ha_dir / d
        if root.is_dir():
            for p in sorted(root.rglob("*")):
                if p.is_file():
                    out[f"ha/{p.relative_to(ha_dir).as_posix()}"] = p.read_bytes()
    return out


def encode_deks(deks: dict[str, bytes]) -> bytes:
    payload = {"format": 1, "deks": {k: base64.b64encode(v).decode() for k, v in deks.items()}}
    return json.dumps(payload, sort_keys=True).encode()


def decode_deks(data: bytes) -> dict[str, bytes]:
    payload = cast(dict[str, object], json.loads(data))
    raw = payload.get("deks")
    if payload.get("format") != 1 or not isinstance(raw, dict):
        msg = "dek.json has an unknown format"
        raise BackupError(msg)
    return {str(k): base64.b64decode(str(v)) for k, v in cast(dict[object, object], raw).items()}


async def build_bundle(src: BackupSources, *, now: datetime) -> tuple[bytes, BundleManifest]:
    """Steps 1-3: checkpoint, ``VACUUM INTO``, and the tar (in memory) with its manifest."""
    await src.db.checkpoint()
    tmp = src.backups_dir / "tmp" / "beepboop.db"
    tmp.unlink(missing_ok=True)
    await src.db.vacuum_into(tmp)
    try:
        db_bytes = tmp.read_bytes()
    finally:
        tmp.unlink(missing_ok=True)
    salt_path = src.keys_dir / "device_salt"
    members: dict[str, bytes] = {"beepboop.db": db_bytes}
    if salt_path.is_file():
        members[DEVICE_SALT] = salt_path.read_bytes()
    members["dek.json"] = encode_deks(await src.secrets.export_deks())
    members.update(_ha_members(src.ha_dir))
    manifest = BundleManifest(
        user_version=await src.db.schema_version(),
        core_version=src.core_version,
        ha_version=src.ha_version,
        created_at=now.astimezone(UTC),
        host=src.host,
        files={name: _sha256(data) for name, data in sorted(members.items())},
    )
    buf = io.BytesIO()
    mtime = int(now.timestamp())
    with tarfile.open(fileobj=buf, mode="w") as tar:
        _add(tar, "manifest.json", manifest.model_dump_json(indent=2).encode(), mtime)
        for name, data in sorted(members.items()):
            _add(tar, name, data, mtime)
    return buf.getvalue(), manifest


def encrypt(plaintext: bytes, recipients: Sequence[str]) -> bytes:
    """Step 4: ``age -r <recipient> [-r …]``."""
    if not recipients:
        msg = "no recovery key is set; create one on the Backups page"
        raise BackupError(msg)
    parsed = [pyrage.x25519.Recipient.from_str(validate_recipient(r)) for r in recipients]
    return pyrage.encrypt(plaintext, parsed)


def decrypt(ciphertext: bytes, identity: str) -> bytes:
    """Open a bundle with the recovery phrase (the ``AGE-SECRET-KEY-1…`` text)."""
    try:
        ident = pyrage.x25519.Identity.from_str(identity.strip())
    except Exception as exc:
        msg = "that is not a recovery key (it starts with AGE-SECRET-KEY-1)"
        raise BackupError(msg) from exc
    try:
        return pyrage.decrypt(ciphertext, [ident])
    except pyrage.DecryptError as exc:
        msg = "this recovery key does not open this backup"
        raise BackupError(msg) from exc


def write_verified(destination: Path, name: str, data: bytes) -> Path:
    """Step 5 for one destination: atomic write, fsync, then re-read and compare."""
    destination.mkdir(parents=True, exist_ok=True)
    final = destination / name
    tmp = destination / f".{name}.partial"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(final)
    written = final.read_bytes()
    if len(written) != len(data) or _sha256(written) != _sha256(data):
        final.unlink(missing_ok=True)
        msg = f"verification failed at {destination}"
        raise BackupError(msg)
    return final


# ------------------------------------------------------------------ pruning --


def keep_set(stamps: Iterable[datetime]) -> set[datetime]:
    """Which bundles survive step 6: the newest per day for the last ``KEEP_DAILY`` days
    that have one, and the newest per ISO week for the last ``KEEP_WEEKLY`` weeks."""
    ordered = sorted(set(stamps), reverse=True)
    keep: set[datetime] = set()
    days: dict[date, datetime] = {}
    weeks: dict[tuple[int, int], datetime] = {}
    for ts in ordered:
        days.setdefault(ts.date(), ts)
        iso = ts.isocalendar()
        weeks.setdefault((iso.year, iso.week), ts)
    keep.update(list(days.values())[:KEEP_DAILY])
    keep.update(list(weeks.values())[:KEEP_WEEKLY])
    return keep


def parse_bundle_name(name: str) -> tuple[str, datetime] | None:
    m = _BUNDLE_NAME.match(name)
    if m is None:
        return None
    ts = datetime.strptime(m["ts"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    return m["host"], ts


def prune(destination: Path, host: str) -> list[Path]:
    """Delete this host's bundles at ``destination`` that :func:`keep_set` drops."""
    found: dict[datetime, Path] = {}
    for p in destination.glob("beepboop-*.tar.age"):
        parsed = parse_bundle_name(p.name)
        if parsed is not None and parsed[0] == host:
            found[parsed[1]] = p
    keep = keep_set(found)
    removed: list[Path] = []
    for ts, p in sorted(found.items()):
        if ts not in keep:
            p.unlink(missing_ok=True)
            removed.append(p)
    return removed


# -------------------------------------------------------------------- run --


async def run_backup(
    src: BackupSources,
    *,
    now: datetime,
    recipients: Sequence[str] | None = None,
    destinations: Sequence[Path] | None = None,
) -> BackupResult:
    """The whole §11 procedure. Never raises for a destination failure: the result says.

    ``recipients``/``destinations`` default to the household settings; the local
    ``backups_dir`` is always the first destination.
    """
    log = get_logger(__name__)
    recips = list(recipients) if recipients is not None else await src.db.read(get_recipients)
    extra = list(destinations) if destinations is not None else await src.db.read(get_destinations)
    name = bundle_name(src.host, now)
    try:
        test_recipient = await ensure_test_identity(src.secrets)
        plaintext, _manifest = await build_bundle(src, now=now)
        sealed = encrypt(plaintext, [*recips, test_recipient] if recips else [])
        del plaintext
    except BackupError as exc:
        result = BackupResult(
            False, name, "", 0, [DestinationResult("build", False, error=str(exc))]
        )
        await _record(src.db, result, now)
        log.warning("backup.fail", error=str(exc))
        return result
    digest = _sha256(sealed)
    results: list[DestinationResult] = []
    for dest in [src.backups_dir, *extra]:
        try:
            path = write_verified(dest, name, sealed)
            removed = prune(dest, src.host)
            results.append(DestinationResult(str(dest), True, str(path), pruned=len(removed)))
        except (OSError, BackupError) as exc:
            results.append(DestinationResult(str(dest), False, error=type(exc).__name__))
    result = BackupResult(all(r.ok for r in results), name, digest, len(sealed), results)
    await _record(src.db, result, now)
    log.info("backup.ok" if result.ok else "backup.fail", name=name, size=len(sealed))
    return result


async def _record(db: Database, result: BackupResult, now: datetime) -> None:
    action = "backup.ok" if result.ok else "backup.fail"
    details: dict[str, object] = result.as_dict(now)

    def _write(conn: sqlite3.Connection) -> None:
        repo.household.set_(conn, LAST_KEY, details)
        repo.audit.record(
            conn, actor_id=None, actor_name="system", via="system", action=action,
            target=result.name, details=details,
        )  # fmt: skip

    await db.write(_write, durable=True)


# ------------------------------------------------------------------ restore --


@dataclass(frozen=True, slots=True)
class OpenedBundle:
    manifest: BundleManifest
    members: dict[str, bytes]


def open_bundle(ciphertext: bytes, identity: str) -> OpenedBundle:
    """Decrypt, untar in memory and verify every member against the manifest."""
    plaintext = decrypt(ciphertext, identity)
    members: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(plaintext), mode="r:") as tar:
        for info in tar.getmembers():
            path = PurePosixPath(info.name)
            if not info.isfile() or path.is_absolute() or ".." in path.parts:
                msg = f"unexpected entry in bundle: {info.name}"
                raise BackupError(msg)
            handle = tar.extractfile(info)
            if handle is None:  # pragma: no cover - isfile() above
                msg = f"unreadable entry in bundle: {info.name}"
                raise BackupError(msg)
            members[info.name] = handle.read()
    raw_manifest = members.pop("manifest.json", None)
    if raw_manifest is None:
        msg = "bundle has no manifest.json"
        raise BackupError(msg)
    manifest = BundleManifest.model_validate_json(raw_manifest)
    if manifest.format != BUNDLE_FORMAT:
        msg = f"bundle format {manifest.format} is not supported"
        raise BackupError(msg)
    if set(members) != set(manifest.files):
        msg = "bundle contents do not match its manifest"
        raise BackupError(msg)
    for name, digest in manifest.files.items():
        if _sha256(members[name]) != digest:
            msg = f"bundle member {name} is corrupt"
            raise BackupError(msg)
    for required in ("beepboop.db", "dek.json"):
        if required not in members:
            msg = f"bundle has no {required}"
            raise BackupError(msg)
    return OpenedBundle(manifest, members)


@dataclass(frozen=True, slots=True)
class RestoreReport:
    user_version_from: int
    user_version_to: int
    deks_imported: int
    ha_files: int
    previous_db: Path | None
    host: str
    created_at: datetime


def _remove_db_files(db_path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)


def _snapshot_closed_db(db_path: Path, target_dir: Path) -> Path | None:
    if not db_path.exists():
        return None
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "beepboop.db"
    target.unlink(missing_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("VACUUM INTO ?", (str(target),))
    finally:
        conn.close()
    return target


def restore_bundle(
    ciphertext: bytes,
    identity: str,
    *,
    data_dir: Path,
    backups_dir: Path,
    ha_dir: Path | None,
    kek: Callable[[], Kek],
    now: datetime,
    migrations_dir: Path = MIGRATIONS_DIR,
) -> RestoreReport:
    """Restore onto this Pi (§11). Core's database must be closed (the host stopped core).

    Order: verify the bundle and its manifest; refuse a newer schema; snapshot the
    current database to ``backups/pre-restore-<ts>/``; swap in the bundle's database;
    migrate forward; re-wrap every DEK under this Pi's KEK (``kek()``); replace HA's
    ``.storage`` and ``configuration.yaml``. ``keys/device_salt`` is **not** restored:
    this Pi's salt wraps its own KEK, and the DEKs are re-wrapped under that KEK.
    """
    opened = open_bundle(ciphertext, identity)
    code_version = latest_version(migrations_dir)
    if opened.manifest.user_version > code_version:
        msg = (
            f"this backup is from a newer beepboop (schema {opened.manifest.user_version}, "
            f"this code knows {code_version}); update first, then restore"
        )
        raise SchemaTooNewError(msg)
    deks = decode_deks(opened.members["dek.json"])
    live_kek = kek()  # fail before touching anything if this Pi has no KEK

    db_path = data_dir / "beepboop.db"
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    previous = _snapshot_closed_db(db_path, backups_dir / f"pre-restore-{stamp}")
    staged = data_dir / "beepboop.db.restore"
    staged.unlink(missing_ok=True)
    staged.write_bytes(opened.members["beepboop.db"])
    _remove_db_files(db_path)
    staged.replace(db_path)

    conn = connect(db_path)
    try:
        migrate(conn, directory=migrations_dir, backups_dir=backups_dir)
        to_version = user_version(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            imported = import_deks(conn, deks, live_kek)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    ha_count = _restore_ha(opened.members, ha_dir)
    return RestoreReport(
        user_version_from=opened.manifest.user_version,
        user_version_to=to_version,
        deks_imported=imported,
        ha_files=ha_count,
        previous_db=previous,
        host=opened.manifest.host,
        created_at=opened.manifest.created_at,
    )


def _restore_ha(members: dict[str, bytes], ha_dir: Path | None) -> int:
    ha = {k.removeprefix("ha/"): v for k, v in members.items() if k.startswith("ha/")}
    if ha_dir is None or not ha:
        return 0
    for d in _HA_DIRS:
        shutil.rmtree(ha_dir / d, ignore_errors=True)
    for rel, data in ha.items():
        target = ha_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return len(ha)


# ------------------------------------------------- the api's BackupRunner --


@dataclass
class BundleBackups:
    """``beepboop.api.ports.BackupRunner`` over :func:`run_backup` (``Back up now``)."""

    sources: BackupSources

    async def run(self, now: datetime) -> Path:
        result = await run_backup(self.sources, now=now)
        if not result.ok:
            errors = "; ".join(d.error or "failed" for d in result.destinations if not d.ok)
            raise BackupError(errors or "backup failed")
        return self.sources.backups_dir / result.name

    def list(self) -> list[Path]:
        d = self.sources.backups_dir
        if not d.is_dir():
            return []
        mine = [p for p in d.glob("beepboop-*.tar.age") if parse_bundle_name(p.name)]
        return sorted(mine, key=lambda p: p.name, reverse=True)


# ------------------------------------------------------ weekly restore check --

CheckOutcome = Literal["ok", "fail", "none"]


async def restore_check(store: SecretStore, backups_dir: Path, host: str) -> CheckOutcome:
    """Weekly: open the newest local bundle with the Pi-held test identity and run
    ``PRAGMA integrity_check`` on its database in a temp file, then delete it."""
    newest: tuple[datetime, Path] | None = None
    candidates = await asyncio.to_thread(lambda: list(backups_dir.glob("beepboop-*.tar.age")))
    for p in candidates:
        parsed = parse_bundle_name(p.name)
        if parsed and parsed[0] == host and (newest is None or parsed[1] > newest[0]):
            newest = (parsed[1], p)
    if newest is None:
        return "none"
    if not await store.exists(TEST_IDENTITY):
        return "fail"
    identity = (await store.get(TEST_IDENTITY)).get_secret_value()
    try:
        opened = open_bundle(newest[1].read_bytes(), identity)
    except BackupError:
        return "fail"
    scratch = backups_dir / "tmp" / "restore-check.db"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_bytes(opened.members["beepboop.db"])
    try:
        conn = sqlite3.connect(scratch)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
    finally:
        scratch.unlink(missing_ok=True)
    return "ok" if row is not None and row[0] == "ok" else "fail"


__all__ = [
    "BUNDLE_FORMAT",
    "DESTINATIONS_KEY",
    "LAST_CHECK_KEY",
    "LAST_KEY",
    "RECIPIENTS_KEY",
    "TEST_IDENTITY",
    "BackupError",
    "BackupResult",
    "BackupSources",
    "BundleBackups",
    "BundleManifest",
    "DestinationResult",
    "OpenedBundle",
    "RecoveryKey",
    "RestoreReport",
    "SchemaTooNewError",
    "build_bundle",
    "bundle_name",
    "decrypt",
    "encrypt",
    "ensure_test_identity",
    "generate_recovery_key",
    "get_destinations",
    "get_recipients",
    "keep_set",
    "open_bundle",
    "parse_bundle_name",
    "prune",
    "restore_bundle",
    "restore_check",
    "run_backup",
    "set_recipients",
    "validate_recipient",
]
