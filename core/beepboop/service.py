"""How core starts (docs/ARCHITECTURE.md §2, §11; MODULES §3): the wiring, in order.

::

    config  →  Database.open (migrate)  →  secrets (KEK, SecretStore)  →  PolicyState
            →  Registry (+ find_tools/read_guide)  →  discover modules/<id>, setup() hooks
            →  HA client  →  agent loop  →  audio bridge  →  FastAPI app  →  nightly scheduler

``CoreService.start()`` is the one place that builds these; :func:`serve` (``beepboop
serve``) starts them and runs three long-lived tasks: uvicorn, the scheduler and the
audio connection (``CoreEventClient`` → :class:`~beepboop.audio_bridge.AudioBridge`,
reconnecting with backoff). Every external dependency is optional at boot: no HA token →
HA stays down; no LLM credentials → ``NoLLM`` (fast path only); no audio service → the
bridge drops speech and keeps reconnecting. So the service boots on x86 with zero hardware.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
import sys
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, time, tzinfo
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from pydantic import SecretStr

from beepboop import __version__
from beepboop.agent import ListSpeaker, TurnLoop
from beepboop.agent.context import VOICE_CONTRACT
from beepboop.agent.types import ConversationStore, Speaker
from beepboop.agent.wiring import build_turn_loop
from beepboop.api.app import create_app
from beepboop.api.claim import ClaimManager
from beepboop.api.deps import ApiDeps
from beepboop.api.events import EventBus, PublishingApprovalService
from beepboop.api.ports import DIGEST_KEYS, NoUpdateSource, ReleaseManifestSource
from beepboop.api.routers.system import ca_fingerprint
from beepboop.api.security import DekColumnSealer
from beepboop.audio_bridge import AudioBridge, DbPinStore, DbVoiceprintSink
from beepboop.db import Database, repo
from beepboop.db.backup import (
    LAST_CHECK_KEY,
    BackupSources,
    BundleBackups,
    restore_check,
    run_backup,
)
from beepboop.db.repo.approvals import DbApprovalStore
from beepboop.db.repo.policy_view import load_policy_state
from beepboop.db.retention import nightly_purge
from beepboop.ha.onboarding import HA_TOKEN_SECRET
from beepboop.ha.service import DEFAULT_WS_URL, HAService, token_from_secrets
from beepboop.health import BedrockStatus, HealthProbes
from beepboop.log import get_logger
from beepboop.module.context import ToolContext
from beepboop.module.types import (
    AnnouncePriority,
    EntityRef,
    EntityState,
    HttpClient,
    ProfileSummary,
    ServiceResult,
    ToolError,
)
from beepboop.paths import default_data_dir
from beepboop.policy.authorize import PolicyState
from beepboop.policy.ratelimit import RateLimiter
from beepboop.policy.types import DEFAULT_CALIBRATION, TIER, Approval, Principal
from beepboop.registry import (
    FIND_TOOLS_SPEC,
    READ_GUIDE_SPEC,
    RegisteredTool,
    Registry,
    ToolIndex,
    discover,
    make_find_tools,
    make_read_guide,
)
from beepboop.registry.registry import ToolCallAudit
from beepboop.scheduler import Job, Scheduler, nightly_jobs
from beepboop.secrets import (
    SecretRef,
    SecretStore,
    keyring_empty,
    read_otp_key,
    read_passphrase_file,
    resolve_kek,
)
from beepboop.service_http import HttpxClient
from beepboop_protocol import (
    DEFAULT_ADDRESS,
    CoreEventClient,
    SocketAddress,
    new_ulid,
    parse_address,
)


@dataclass(frozen=True, slots=True)
class CoreConfig:
    """Paths and ids core needs at start (ARCHITECTURE §9: the file holds hardware and
    provider ids, the database holds everything the UI edits)."""

    data_dir: Path
    modules_dir: Path
    bind_host: str = "127.0.0.1"
    bind_port: int = 8080
    public_url: str = "https://beepboop.local"
    device_id: str = "main"
    locale: str = "en-US"
    timezone: str = "UTC"
    kek_passphrase_file: Path | None = None
    spa_dir: Path | None = None
    #: ``/backups`` and ``/ha`` in the container (OPERATIONS §3); dev defaults under data_dir.
    backups_dir: Path | None = None
    ha_dir: Path | None = None
    #: The host's ``BB_HOST`` (bundle names, DATA_AND_PRIVACY §11).
    host: str = "beepboop.local"
    #: ``BB_AUDIO_SOCKET``: where the audio service listens (ARCHITECTURE §4.1); ``None``
    #: runs core without an audio connection (tests, ``beepboop chat``).
    audio_socket: str | None = None
    #: ``BB_HA_URL``: HA's WebSocket (HOME_ASSISTANT §2).
    ha_url: str = DEFAULT_WS_URL
    #: ``BB_HOST_JSON``: the host timer's status file (OPERATIONS §5); ``BB_CA_CERT``: the
    #: Tier-0 root certificate for ``/ca`` and the health page's expiry (WEB_UI §7).
    host_json: Path | None = None
    ca_cert: Path | None = None
    #: ``BB_UPDATE_MANIFEST``: the release manifest (file or https URL) the daily
    #: ``updates.check`` compares against the running ``BB_*_DIGEST`` values (OPERATIONS §9).
    update_manifest: str | None = None
    digests: Mapping[str, str] = field(default_factory=dict[str, str])

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> CoreConfig:
        e = env if env is not None else dict(os.environ)
        data_dir = default_data_dir(e)
        repo_root = Path(__file__).resolve().parents[2]
        return cls(
            data_dir=data_dir,
            modules_dir=Path(e.get("BB_MODULES_DIR", str(repo_root / "modules"))),
            bind_host=e.get("BB_BIND_HOST", "127.0.0.1"),
            bind_port=int(e.get("BB_BIND_PORT", "8080")),
            public_url=e.get("BB_PUBLIC_URL", "https://beepboop.local"),
            device_id=e.get("BB_DEVICE_ID", "main"),
            locale=e.get("BB_LOCALE", "en-US"),
            timezone=e.get("BB_TZ", "UTC"),
            kek_passphrase_file=(
                Path(e["BB_KEK_PASSPHRASE_FILE"]) if "BB_KEK_PASSPHRASE_FILE" in e else None
            ),
            spa_dir=Path(e["BB_SPA_DIR"]) if "BB_SPA_DIR" in e else None,
            backups_dir=Path(e["BB_BACKUPS_DIR"]) if "BB_BACKUPS_DIR" in e else None,
            ha_dir=Path(e["BB_HA_DIR"]) if "BB_HA_DIR" in e else None,
            host=e.get("BB_HOST", "beepboop.local"),
            audio_socket=e.get("BB_AUDIO_SOCKET") or None,
            ha_url=e.get("BB_HA_URL", DEFAULT_WS_URL),
            host_json=Path(e["BB_HOST_JSON"]) if "BB_HOST_JSON" in e else None,
            ca_cert=Path(e["BB_CA_CERT"]) if "BB_CA_CERT" in e else None,
            update_manifest=e.get("BB_UPDATE_MANIFEST") or None,
            digests={k: e[k] for k in DIGEST_KEYS if e.get(k)},
        )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "beepboop.db"

    @property
    def backups_path(self) -> Path:
        return self.backups_dir or self.data_dir / "backups"

    @property
    def keys_dir(self) -> Path:
        return self.data_dir / "keys"

    @property
    def tz(self) -> tzinfo:
        return ZoneInfo(self.timezone)

    @property
    def audio_address(self) -> SocketAddress | None:
        """The parsed ``BB_AUDIO_SOCKET`` (``unix://…`` or dev ``tcp://127.0.0.1:<port>``)."""
        if self.audio_socket is None:
            return None
        return parse_address(self.audio_socket or DEFAULT_ADDRESS)


# ------------------------------------------------------------ stand-ins / adapters --


class UnavailableHA:
    """``HAClient`` until the ``ha`` builder wires ``core/beepboop/ha`` and the policy wrapper."""

    async def resolve(
        self, target: str, *, domains: list[str] | None = None, area: str | None = None
    ) -> list[EntityRef]:
        return []

    async def state(self, entity: EntityRef | str) -> EntityState | None:
        return None

    async def states(self, domains: list[str] | None = None) -> list[EntityState]:
        return []

    async def call_service(
        self,
        domain: str,
        service: str,
        targets: list[EntityRef],
        data: dict[str, Any] | None = None,
    ) -> ServiceResult:
        raise ToolError("Home Assistant isn't connected.", code="unavailable")


@dataclass
class DbKV:
    """``KVStore`` over ``repo.kv`` for one (module, profile) scope."""

    db: Database
    module_id: str
    profile_id: str | None

    async def get(self, key: str, default: Any = None) -> Any:
        value = await self.db.read(lambda c: repo.kv.get(c, self.module_id, self.profile_id, key))
        return default if value is None else value

    async def set(self, key: str, value: Any) -> None:
        await self.db.write(lambda c: repo.kv.set_(c, self.module_id, self.profile_id, key, value))

    async def delete(self, key: str) -> None:
        await self.db.write(lambda c: repo.kv.delete(c, self.module_id, self.profile_id, key))

    async def keys(self, prefix: str = "") -> list[str]:
        return await self.db.read(
            lambda c: repo.kv.keys(c, self.module_id, self.profile_id, prefix)
        )

    def household(self) -> DbKV:
        return DbKV(self.db, self.module_id, None)


@dataclass
class ScopedSecrets:
    """``SecretStore`` (module-facing) over the envelope store, scoped to (module, profile)."""

    store: SecretStore | None
    module_id: str
    profile_id: str | None

    def _ref(self, name: str) -> SecretRef:
        return SecretRef(name, owner_module=self.module_id, profile_id=self.profile_id)

    async def get(self, name: str) -> str | None:
        if self.store is None or not await self.store.exists(self._ref(name)):
            return None
        return (await self.store.get(self._ref(name))).get_secret_value()

    async def set(self, name: str, value: str) -> None:
        if self.store is None:
            raise ToolError("Secrets are not available.", code="unavailable")
        await self.store.put(self._ref(name), SecretStr(value))

    async def delete(self, name: str) -> None:
        if self.store is not None:
            await self.store.delete(self._ref(name))


def should_flag(entry: ToolCallAudit, state: PolicyState | None) -> bool:
    """IDENTITY_AND_TRUST §11 ``flagged``: a ``personal``+ call ran below τ_strict; a deny
    for ``ceiling`` or ``impersonation``; a tainted turn executed a non-read tool."""
    d = entry.decision
    if d.outcome == "deny":
        return d.reason in ("ceiling", "impersonation")
    if d.outcome != "allow" or entry.result_status in ("denied", "cancelled"):
        return False
    p = entry.principal
    if p.tainted and TIER[d.effective_risk] > TIER["read"]:
        return True
    if p.via == "voice" and TIER[d.effective_risk] >= TIER["personal"]:
        profile = None if state is None else state.profiles.get(p.profile_id)
        cal = profile.calibration if profile is not None else DEFAULT_CALIBRATION
        return (p.confidence if p.confidence is not None else 0.0) < cal.tau_personal
    return False


class DbAudit:
    """``AuditSink`` writing ``tool_calls`` rows (trace writes, coalesced) and the §11
    ``flagged`` mark on the turn."""

    def __init__(self, db: Database, policy_state: Callable[[], PolicyState] | None = None) -> None:
        self.db = db
        self.policy_state = policy_state
        self._seq: dict[str, int] = {}

    async def record(self, entry: ToolCallAudit) -> None:
        seq = self._seq[entry.turn_id] = self._seq.get(entry.turn_id, 0) + 1
        flag = should_flag(entry, None if self.policy_state is None else self.policy_state())
        row = repo.turns.ToolCallRow(
            turn_id=entry.turn_id,
            seq=seq,
            tool=entry.tool,
            module_id=entry.module_id,
            args_redacted=entry.args_redacted,
            effective_risk=entry.decision.effective_risk,
            decision=entry.decision.outcome,
            result_status=entry.result_status,  # pyright: ignore[reportArgumentType]
            profile_id=entry.principal.profile_id,
            targets=[{"uuid": t.uuid, "domain": t.domain} for t in entry.targets],
            deny_reason=entry.decision.reason,
            grant_id=entry.decision.grant_id,
            approval_id=entry.approval_id,
            untrusted_output=entry.untrusted_output,
            duration_ms=entry.duration_ms,
        )

        # The turns row is written at end of turn by the loop; a tool_calls row needs it, so
        # the agent builder inserts the turn first (turns.insert_turn) and then flushes these.
        def _write(c: sqlite3.Connection) -> None:
            repo.turns.insert_tool_call(c, row)
            if flag:
                repo.turns.set_flagged(c, row.turn_id)

        await self.db.write(_write)


@dataclass
class CoreContextFactory:
    """Builds ``ToolContext`` for hooks and calls from the live services (MODULES §5.3)."""

    db: Database
    secrets: SecretStore | None
    ha: Any
    # (egress destinations, the context's settings): ``setting:<name>`` egress entries resolve
    # against the settings (MODULES §2), e.g. caldav's ``setting:url``.
    http_factory: Callable[[list[str], Mapping[str, Any]], HttpClient]
    announce: Callable[..., Coroutine[Any, Any, None]]
    tz: tzinfo
    locale: str
    registry: Registry | None = None
    #: ``ctx.ha`` for one call: the policy-wrapped client bound to the caller, tool and
    #: verified approval (HOME_ASSISTANT §8). ``None`` keeps the hook client (``self.ha``).
    ha_for_call: (
        Callable[[Principal, RegisteredTool, Approval | None, float | None], Any] | None
    ) = None
    _tasks: list[asyncio.Task[Any]] = field(default_factory=list[asyncio.Task[Any]])
    #: Saved settings per (module, profile or None) from ``integrations.settings_json``;
    #: reloaded by :meth:`load_settings` (at start and on every ``refresh_policy``).
    _stored: dict[tuple[str, str | None], dict[str, Any]] = field(
        default_factory=dict[tuple[str, str | None], dict[str, Any]]
    )

    async def load_settings(self) -> None:
        rows = await self.db.read(repo.integrations.list_all)
        self._stored = {(r.module_id, r.profile_id): dict(r.settings) for r in rows}

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._tasks.append(task)
        return task

    def _settings(self, module_id: str, profile_id: str | None) -> dict[str, Any]:
        """Schema defaults, then the household row, then the profile's row (MODULES §2)."""
        manifest = self.registry.manifest(module_id) if self.registry else None
        merged: dict[str, Any] = {}
        if manifest is not None:
            props: object = manifest.settings.get("properties", {})
            if isinstance(props, dict):
                for k, v in cast(dict[str, Any], props).items():
                    if isinstance(v, dict) and "default" in v:
                        merged[str(k)] = cast(dict[str, Any], v)["default"]
        merged.update(self._stored.get((module_id, None), {}))
        if profile_id is not None:
            merged.update(self._stored.get((module_id, profile_id), {}))
        return merged

    def for_hook(self, module_id: str, profile_id: str | None = None) -> ToolContext:
        manifest = self.registry.manifest(module_id) if self.registry else None
        egress = [e.destination for e in manifest.egress] if manifest else []
        settings = self._settings(module_id, profile_id)
        return ToolContext(
            module_id=module_id,
            profile_id=profile_id,
            profile=None,
            settings=settings,
            secrets=ScopedSecrets(self.secrets, module_id, profile_id),
            ha=self.ha,
            http=self.http_factory(egress, settings),
            kv=DbKV(self.db, module_id, profile_id),
            log=get_logger("module").bind(module_id=module_id, profile_id=profile_id),
            locale=self.locale,
            tz=self.tz,
            now=lambda: datetime.now(UTC),
            dry_run=False,
            spawn=self._spawn,
            announce=self.announce,
        )

    def for_call(
        self,
        tool: RegisteredTool,
        principal: Principal,
        *,
        deadline: float | None,
        approval: Approval | None,
    ) -> ToolContext:
        manifest = self.registry.manifest(tool.module_id) if self.registry else None
        profile_scope = manifest is not None and manifest.scope == "profile"
        base = self.for_hook(tool.module_id, principal.profile_id if profile_scope else None)
        # The profile summary is a placeholder until the api/people builder adds a cached
        # profiles lookup; name/kind come from repo.profiles then.
        profile = ProfileSummary(id=principal.profile_id, name=principal.profile_id, kind="member")
        ctx = base.with_call(
            principal=principal,
            grants=None,
            turn_id=principal.turn_id,
            deadline=deadline,
            approval=approval,
        )
        # In a tool call ctx.kv is always the (module, caller) scope (MODULES §5.3).
        ha = (
            ctx.ha
            if self.ha_for_call is None
            else self.ha_for_call(principal, tool, approval, deadline)
        )
        return replace(
            ctx,
            profile=profile,
            profile_id=principal.profile_id,
            kv=DbKV(self.db, tool.module_id, principal.profile_id),
            ha=ha,
        )

    async def cancel_all(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks.clear()


# ------------------------------------------------------------------------ secrets --


async def open_secrets(db: Database, config: CoreConfig) -> SecretStore:
    """The KEK for this board, then the store over the core database (DATA_AND_PRIVACY §6.3).

    On first boot (empty keyring) the passphrase-wrapped KEK file is created from the
    installer's passphrase file; once DEKs exist it is never recreated, so a lost
    ``keys/kek.passphrase.wrapped`` fails loudly instead of stranding the keyring.
    """
    passphrase = (
        read_passphrase_file(config.kek_passphrase_file)
        if config.kek_passphrase_file is not None
        else None
    )
    first_boot = await db.read(keyring_empty)
    kek = resolve_kek(
        config.keys_dir,
        otp_reader=read_otp_key,
        passphrase=passphrase,
        create_passphrase_file=first_boot,
    )
    return await SecretStore.attach(db, kek)


#: OPERATIONS §9 step 1: the daily read-only update check, after the backup window.
UPDATES_CHECK_AT = time(4, 30)


def _bedrock_probe(loop: TurnLoop, db: Database) -> Callable[[], Awaitable[BedrockStatus]]:
    """OPERATIONS §11: LLM reachable (the ladder's reachability, ``None`` when no provider
    is configured) and the last measured TTFT from the newest LLM turn."""

    async def probe() -> BedrockStatus:
        reachable: bool | None = None
        ladder = loop.ladder
        if ladder is not None:
            try:
                reachable = await ladder.reachability.reachable()
            except Exception:  # pragma: no cover - a probe never breaks the health page
                reachable = None
        row = await db.read(
            lambda c: c.execute(
                "SELECT stages_ms_json FROM turns WHERE llm_provider IS NOT NULL"
                " ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        )
        ttft: float | None = None
        if row is not None:
            stages = cast(dict[str, object], json.loads(str(row[0]) or "{}"))
            value = stages.get("llm_ttft")
            ttft = float(value) if isinstance(value, int | float) else None
        return BedrockStatus(reachable=reachable, last_ttft_ms=ttft)

    return probe


def _update_probe(
    updates: ReleaseManifestSource | None,
) -> Callable[[], Awaitable[str | None]] | None:
    if updates is None:
        return None

    async def probe() -> str | None:
        found = updates.last if updates.checked_at is not None else await updates.available()
        return None if found is None else found.get("version")

    return probe


def make_nightly_jobs(
    db: Database,
    secrets: SecretStore | None,
    backups: BundleBackups | None,
    config: CoreConfig,
    updates: ReleaseManifestSource | None = None,
) -> list[Job]:
    """DATA_AND_PRIVACY §11: 03:00 purge, 03:30 backup, weekly restore check; OPERATIONS §9:
    the daily read-only ``updates.check`` (04:30) when a release manifest is configured."""

    async def purge(now: datetime) -> object:
        return await db.write(lambda c: nightly_purge(c, now), durable=True)

    async def backup(now: datetime) -> object:
        if backups is None:  # pragma: no cover - only scheduled when secrets exist
            return None
        return await run_backup(backups.sources, now=now)

    async def restore_check_job(now: datetime) -> object:
        if secrets is None:  # pragma: no cover - only scheduled when secrets exist
            return None
        outcome = await restore_check(secrets, config.backups_path, config.host)
        await db.write(
            lambda c: repo.household.set_(
                c, LAST_CHECK_KEY, {"at": now.isoformat(), "outcome": outcome}
            ),
            durable=True,
        )
        return outcome

    async def updates_check(now: datetime) -> object:
        if updates is None:  # pragma: no cover - only scheduled when configured
            return None
        return await updates.check(now)

    jobs = nightly_jobs(
        purge=purge,
        backup=backup if backups is not None else None,
        restore_check=restore_check_job if secrets is not None else None,
    )
    if updates is not None:
        jobs.append(Job("updates.check", UPDATES_CHECK_AT, updates_check))
    return jobs


# ------------------------------------------------------------------------ service --


@dataclass
class CoreService:
    """Owns every long-lived object. Build with :meth:`start`; stop with :meth:`stop`."""

    config: CoreConfig
    db: Database
    registry: Registry
    policy_state: Callable[[], PolicyState]
    refresh_policy: Callable[[], Coroutine[Any, Any, None]]
    loop: TurnLoop
    app: FastAPI
    contexts: CoreContextFactory
    secrets: SecretStore | None
    index: ToolIndex
    modules_errors: dict[str, str]
    #: Nightly purge / backup / weekly restore check; started by :func:`serve`.
    scheduler: Scheduler | None = None
    #: The API's dependencies (event bus, claim code, enrollment hub) for the audio bridge.
    api: ApiDeps | None = None
    #: Home Assistant: connection, state cache, registry index, inbox mirror (HOME_ASSISTANT §4-§8).
    ha: HAService | None = None
    #: Core's end of the audio socket (speech, turns, enrollment, profiles.sync).
    audio: AudioBridge | None = None

    @classmethod
    async def start(
        cls,
        config: CoreConfig,
        *,
        speaker: Speaker | None = None,
        conversation: ConversationStore | None = None,
    ) -> CoreService:
        log = get_logger(__name__)
        db = await Database.open(config.db_path, backups_dir=config.backups_path)

        secrets: SecretStore | None = None
        try:
            secrets = await open_secrets(db, config)
        except Exception as exc:
            log.warning("secrets.unavailable", error=type(exc).__name__)

        rate_limiter = RateLimiter()
        state_box: list[PolicyState] = [
            await db.read(lambda c: load_policy_state(c, tz=config.tz, rate_limiter=rate_limiter))
        ]

        contexts_box: list[CoreContextFactory] = []

        async def refresh() -> None:
            state_box[0] = await db.read(
                lambda c: load_policy_state(c, tz=config.tz, rate_limiter=rate_limiter)
            )
            for factory in contexts_box:  # saved integration settings → ctx.settings
                await factory.load_settings()

        events = EventBus()
        # The web second factor: the new approval and its nonce go to the SSE bus.
        approvals = PublishingApprovalService(DbApprovalStore(db), new_ulid, bus=events)
        sealer = None if secrets is None else DekColumnSealer(db, secrets)
        claim = ClaimManager()
        bridge = AudioBridge(db, sealer=sealer, claim=claim, default_locale=config.locale)
        claim.on_code = bridge.on_claim_code

        async def announce(
            text: str, *, profile_id: str | None = None, priority: AnnouncePriority = "normal"
        ) -> None:
            await bridge.announce(text, profile_id=profile_id, priority=priority)

        # HA: the policy-wrapped ctx.ha and the home.*/media.* target extractor. The
        # connection starts only once onboarding has stored the long-lived token.
        ha = HAService(db, lambda: state_box[0], refresh)
        contexts = CoreContextFactory(
            db=db,
            secrets=secrets,
            ha=ha.client_for_hook(),
            ha_for_call=lambda p, t, a, d: ha.client_for_call(p, t, approval=a, deadline=d),
            http_factory=lambda hosts, settings: HttpxClient(
                allowed_hosts=hosts, settings=settings
            ),
            announce=announce,
            tz=config.tz,
            locale=config.locale,
        )
        registry = Registry(
            policy_state=lambda: state_box[0],
            approvals=approvals,
            context_factory=contexts,
            audit=DbAudit(db, lambda: state_box[0]),
            targets=ha.extractor,
        )
        contexts.registry = registry
        contexts_box.append(contexts)
        await contexts.load_settings()
        await ha.load_mirror()  # names resolve from the mirror until HA syncs
        if secrets is not None and await secrets.exists(HA_TOKEN_SECRET):
            store = secrets
            await ha.start(lambda: token_from_secrets(store), url=config.ha_url)
        bridge.entity_index = lambda: ha.index

        index_box: list[ToolIndex] = [ToolIndex()]
        registry.register_builtin(
            FIND_TOOLS_SPEC,
            make_find_tools(
                lambda: index_box[0],
                lambda ctx: (
                    {t.name for t in registry.callable_by(ctx.principal)}
                    if ctx.principal
                    else set()
                ),
            ),
        )
        registry.register_builtin(
            READ_GUIDE_SPEC,
            make_read_guide(
                registry.guide, lambda m: [t.name for t in registry.tools() if t.module_id == m]
            ),
        )

        errors: dict[str, str] = {}
        entries: list[Any] = []
        for found in discover(config.modules_dir):
            if found.error or found.module_class is None:
                errors[found.id] = found.error or "no Module class"
                continue
            instance = found.module_class()
            agent_md = (found.module_dir / "AGENT.md").read_text(encoding="utf-8")
            setup_path = found.module_dir / "SETUP.md"
            setup_md = setup_path.read_text(encoding="utf-8") if setup_path.is_file() else ""
            try:
                await asyncio.wait_for(instance.setup(contexts.for_hook(found.id)), timeout=5)
            except Exception as exc:
                errors[found.id] = f"setup failed: {type(exc).__name__}"
                continue
            registry.register_module(found.manifest, instance, agent_md, setup_md)
            entries.extend((found.manifest, t, agent_md) for t in found.manifest.tools)
        index_box[0] = ToolIndex.build(entries)
        log.info("modules.loaded", loaded=len(registry.modules()), errors=sorted(errors))

        # The agent (agent/wiring.py): ladder from household.llm + secrets (fast-path only
        # until credentials exist), DB conversation window + taint, turns rows, directory.
        loop = await build_turn_loop(
            db=db,
            registry=registry,
            secrets=secrets,
            speaker=speaker or ListSpeaker(),
            conversation=conversation,
            locale=config.locale,
            tz=config.tz,
            hook_context=lambda module_id, profile_id: contexts.for_hook(module_id, profile_id),
            entity_index=lambda: ha.index,
            pins=DbPinStore(db, sealer, on_disabled=refresh),
        )
        if speaker is None:
            bridge.attach(loop)  # the device speaks; tests may pass their own speaker
        else:
            loop.control = bridge
            bridge.loop = loop
        # A web decision wakes the voice turn waiting on it (no poll delay).
        events.approval_listeners.append(loop.notify_approval)
        backups = (
            BundleBackups(
                BackupSources(
                    db=db,
                    secrets=secrets,
                    keys_dir=config.keys_dir,
                    backups_dir=config.backups_path,
                    host=config.host,
                    core_version=__version__,
                    ha_dir=config.ha_dir,
                )
            )
            if secrets is not None
            else None
        )
        updates: ReleaseManifestSource | None = (
            None
            if config.update_manifest is None
            else ReleaseManifestSource(config.update_manifest, dict(config.digests))
        )
        deps = ApiDeps(
            db=db,
            registry=registry,
            policy_state=lambda: state_box[0],
            public_url=config.public_url,
            version=__version__,
            backups=backups,
            updates=updates if updates is not None else NoUpdateSource(),
            refresh_policy=refresh,
            events=events,
            claim=claim,
            sealer=sealer,
            secrets=secrets,
            loop=loop,
            data_dir=config.data_dir,
            enrollment=bridge,
            ca_cert=config.ca_cert,
            host_json=config.host_json,
            health_probes=HealthProbes(
                ha_connected=_ha_probe(ha),
                audio_device_present=bridge.audio_device_present,
                bedrock=_bedrock_probe(loop, db),
                update_available=_update_probe(updates),
            ),
        )
        bridge.ca_fingerprint = lambda: ca_fingerprint(deps)
        if deps.enroll_hub is not None:
            bridge.enroll_hub = deps.enroll_hub
            if sealer is not None:
                deps.enroll_hub.sink = DbVoiceprintSink(
                    db, sealer, bridge.speaker_model, on_saved=bridge.sync_profiles
                )
        app = create_app(deps, spa_dir=config.spa_dir)
        service = cls(
            config, db, registry, lambda: state_box[0], refresh, loop, app, contexts, secrets,
            index_box[0], errors,
        )  # fmt: skip
        service.api = deps
        service.ha = ha
        service.audio = bridge
        service.scheduler = Scheduler(
            make_nightly_jobs(db, secrets, backups, config, updates), config.tz
        )
        return service

    async def stop(self) -> None:
        for manifest, module in self.registry.modules():
            try:
                await asyncio.wait_for(
                    module.teardown(self.contexts.for_hook(manifest.id)), timeout=2
                )
            except Exception:
                get_logger(__name__).warning("module.teardown_failed", module_id=manifest.id)
        if self.scheduler is not None:
            await self.scheduler.stop()
        if self.audio is not None:
            await self.audio.close()
        await self.contexts.cancel_all()
        if self.ha is not None:
            await self.ha.stop()
        if self.secrets is not None:
            await self.secrets.close()
        await self.db.close()


async def serve(config: CoreConfig) -> None:
    """``beepboop serve``: start everything and run uvicorn until cancelled."""
    import uvicorn

    service = await CoreService.start(config)
    audio_task: asyncio.Task[None] | None = None
    expiry_task: asyncio.Task[None] | None = None
    try:
        server = uvicorn.Server(
            uvicorn.Config(
                service.app, host=config.bind_host, port=config.bind_port, log_config=None
            )
        )
        if service.scheduler is not None:
            service.scheduler.start()
        client = audio_client(service)
        if client is not None:
            audio_task = asyncio.create_task(client.run(), name="audio-client")
        if service.api is not None:
            expiry_task = asyncio.create_task(expire_approvals(service.api), name="expiry")
        if service.api is not None:
            from beepboop.api.routers.onboarding import open_first_claim

            # The device speaks the code (audio bridge); without an audio service
            # configured, the dev console shows it instead.
            code = await open_first_claim(service.api)
            if code is not None and client is None:
                sys.stderr.write(f"beepboop: unclaimed; claim code {code} (15 min)\n")
        await server.serve()
    finally:
        for task in (audio_task, expiry_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await service.stop()


#: How often pending approvals past ``exp`` are marked ``expired`` and published.
EXPIRY_SWEEP_S = 5.0


async def expire_approvals(deps: ApiDeps, every_s: float = EXPIRY_SWEEP_S) -> None:
    """IDENTITY_AND_TRUST §8.2 expiry: the banner goes away and the waiting voice turn
    speaks ``approval.expired``. Runs until cancelled; a failed sweep is logged, not fatal."""
    from beepboop.api.routers.approvals import expire_due

    log = get_logger(__name__)
    while True:
        try:
            await expire_due(deps)
        except Exception as exc:
            log.warning("approvals.expiry_failed", error=type(exc).__name__)
        await asyncio.sleep(every_s)


def _ha_probe(ha: HAService) -> Callable[[], Coroutine[Any, Any, bool | None]]:
    async def probe() -> bool | None:
        if ha.connection is None:
            return None  # not onboarded yet: unknown, not "down"
        return ha.health == "ok"

    return probe


def audio_client(service: CoreService) -> CoreEventClient | None:
    """The reconnecting client for ``BB_AUDIO_SOCKET`` (``None`` when unset)."""
    address = service.config.audio_address
    if address is None or service.audio is None:
        return None
    return CoreEventClient(address, service.audio.handle)


__all__ = [
    "VOICE_CONTRACT",
    "CoreConfig",
    "CoreContextFactory",
    "CoreService",
    "DbAudit",
    "DbKV",
    "ScopedSecrets",
    "UnavailableHA",
    "audio_client",
    "expire_approvals",
    "make_nightly_jobs",
    "open_secrets",
    "serve",
]
