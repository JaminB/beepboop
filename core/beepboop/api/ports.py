"""Seams the API needs from other builders, with honest stand-ins (core/README.md).

* :class:`EnrollmentChannel`: the audio bridge sends ``enroll.start`` / ``enroll.stop``
  over the event socket and feeds ``enroll.clip`` results back through
  :meth:`EnrollmentHub.clip`, which turns them into SSE ``enroll`` events. No audio, and
  no embedding, ever reaches the API (ADR 0018); the hub only sees quality numbers.
* :class:`LogSource`: the last structlog lines for System > Logs.
* :class:`BackupRunner`: ``Back up now`` (the ops builder replaces the snapshot-only
  runner with the encrypted bundle).
* :class:`UpdateSource`: notify-only update check (OPERATIONS §9).
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from beepboop.db.database import Database
from beepboop.log import get_logger


class EnrollmentChannel(Protocol):
    """Core → audio enrollment control (VOICE_PIPELINE §9.2, events ``enroll.*``)."""

    @property
    def available(self) -> bool: ...
    async def start(self, session_id: str, profile_id: str, prompts: list[str]) -> None: ...
    async def stop(self, session_id: str) -> None: ...


class NoAudioEnrollment:
    """Stand-in until the audio bridge is wired: enrollment reports ``audio_offline``."""

    @property
    def available(self) -> bool:
        return False

    async def start(self, session_id: str, profile_id: str, prompts: list[str]) -> None:
        raise RuntimeError("audio_offline")

    async def stop(self, session_id: str) -> None:
        return None


class LogSource(Protocol):
    def lines(self, limit: int = 500) -> list[dict[str, Any]]: ...


@dataclass
class RingLog:
    """A structlog processor that keeps the last N (already redacted) event dicts.

    Install it *after* the redaction processor (``beepboop.log``), so what the UI shows is
    exactly what went to stdout.
    """

    size: int = 500
    _lines: deque[dict[str, Any]] = field(default_factory=lambda: deque[dict[str, Any]]())

    def __call__(
        self, _logger: object, _method: str, event_dict: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        self._lines.append(dict(event_dict))
        while len(self._lines) > self.size:
            self._lines.popleft()
        return event_dict

    def lines(self, limit: int = 500) -> list[dict[str, Any]]:
        return list(self._lines)[-limit:]


class BackupRunner(Protocol):
    async def run(self, now: datetime) -> Path: ...
    def list(self) -> list[Path]: ...


@dataclass
class SnapshotBackups:
    """Bare ``VACUUM INTO`` snapshots in ``<data>/backups`` (tests and the dev server).

    Production wires ``BundleBackups`` (``beepboop.db.backup``), the DATA_AND_PRIVACY §11
    bundle; this runner stays for the harness and for ``db backup --out``.
    """

    db: Database
    directory: Path

    async def run(self, now: datetime) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        await self.db.checkpoint()
        return await self.db.vacuum_into(self.directory / f"beepboop-{stamp}.db")

    def list(self) -> list[Path]:
        if not self.directory.is_dir():
            return []
        return sorted(self.directory.glob("beepboop-*.db"), reverse=True)


class UpdateSource(Protocol):
    async def available(self) -> Mapping[str, str] | None: ...


class NoUpdateSource:
    """No release feed configured: Updates shows the current version only."""

    async def available(self) -> Mapping[str, str] | None:
        return None


DIGEST_KEYS = ("BB_CORE_DIGEST", "BB_AUDIO_DIGEST", "BB_HA_DIGEST", "BB_CADDY_DIGEST")
UPDATE_FETCH_TIMEOUT_S = 10.0


@dataclass
class ReleaseManifestSource:
    """OPERATIONS §9 step 1: compare the release manifest's digests with the running
    ones (``BB_*_DIGEST`` in the container environment, from ``deploy/.env``). Read-only:
    nothing is pulled. ``check()`` is the daily ``updates.check`` job; ``available()`` is
    what the Updates page and the health probe read (the last check's result, or a fresh
    check when none has run yet).

    The manifest is a JSON object ``{"release", "digests": {BB_*_DIGEST: sha256:...},
    "changelog_url"?, "schema_version"?}``, the same shape ``beepboop update --manifest``
    reads on the host. Its signature is pending (§9), so ``source`` should be a file the
    admin vetted or the project's HTTPS feed.
    """

    source: str
    current: Mapping[str, str]
    fetch: Callable[[str], Awaitable[Mapping[str, object] | None]] | None = None
    last: Mapping[str, str] | None = None
    checked_at: datetime | None = None
    last_error: str | None = None

    async def _load(self) -> Mapping[str, object] | None:
        if self.source.startswith("https://"):
            if self.fetch is not None:
                return await self.fetch(self.source)
            import httpx

            async with httpx.AsyncClient(timeout=UPDATE_FETCH_TIMEOUT_S) as client:
                response = await client.get(self.source)
                response.raise_for_status()
                body: object = response.json()
                return cast(Mapping[str, object], body) if isinstance(body, Mapping) else None
        text = await asyncio.to_thread(Path(self.source).read_text, encoding="utf-8")
        body = json.loads(text)
        return cast(Mapping[str, object], body) if isinstance(body, Mapping) else None

    async def check(self, now: datetime | None = None) -> Mapping[str, str] | None:
        """Fetch and compare; remembers the outcome. Never raises (the job logs it)."""
        self.checked_at = now or datetime.now(UTC)
        try:
            data = await self._load()
        except Exception as exc:
            self.last_error = type(exc).__name__
            self.last = None
            get_logger(__name__).warning("updates.check_failed", error=self.last_error)
            return None
        self.last_error = None
        if data is None:
            self.last = None
            return None
        raw = data.get("digests")
        digests: dict[str, str] = {}
        if isinstance(raw, Mapping):
            for k, v in cast(Mapping[object, object], raw).items():
                if isinstance(k, str) and isinstance(v, str):
                    digests[k] = v
        differs = any(k in digests and digests[k] != self.current.get(k) for k in DIGEST_KEYS)
        if not differs:
            self.last = None
            return None
        found: dict[str, str] = {"version": str(data.get("release", "?"))}
        changelog = data.get("changelog_url")
        if isinstance(changelog, str):
            found["changelog_url"] = changelog
        self.last = found
        get_logger(__name__).info("updates.available", release=found["version"])
        return found

    async def available(self) -> Mapping[str, str] | None:
        if self.checked_at is None:
            return await self.check()
        return self.last


__all__ = [
    "DIGEST_KEYS",
    "BackupRunner",
    "EnrollmentChannel",
    "LogSource",
    "NoAudioEnrollment",
    "NoUpdateSource",
    "ReleaseManifestSource",
    "RingLog",
    "SnapshotBackups",
    "UpdateSource",
]
