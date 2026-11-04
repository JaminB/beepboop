"""``fake_context``: a dry-run ``ToolContext`` seeded from ``evals.yaml`` (MODULES §12.4)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import structlog
import structlog.testing
from structlog.typing import FilteringBoundLogger

from beepboop.module.context import ToolContext
from beepboop.module.types import ProfileSummary
from beepboop.policy.types import ALL_SCOPES, Principal
from beepboop.registry.manifest import Manifest, load_manifest
from beepboop.testing.evals_model import (
    DEFAULT_PROFILES,
    EvalsFile,
    Fixtures,
    HttpFixtureRow,
    ProfileFixture,
    load_evals,
)
from beepboop.testing.fakes import (
    AllowAllGrants,
    Effect,
    FakeAnnounce,
    FakeHA,
    FakeHttp,
    FakeKV,
    FakeSecrets,
    HAStateFixture,
    HttpFixture,
)


@dataclass(frozen=True, slots=True)
class FakeToolContext(ToolContext):
    """A ``ToolContext`` plus the recorded side effects (``ctx.effects``)."""

    effects: list[Effect] = field(default_factory=list[Effect])
    log_calls: list[structlog.testing.CapturedCall] = field(
        default_factory=list[structlog.testing.CapturedCall]
    )

    @property
    def fake_http(self) -> FakeHttp:
        http = self.http
        if not isinstance(http, FakeHttp):  # pragma: no cover - always true for fake_context
            msg = "ctx.http is not the fake"
            raise TypeError(msg)
        return http

    @property
    def fake_kv(self) -> FakeKV:
        kv = self.kv
        if not isinstance(kv, FakeKV):  # pragma: no cover
            msg = "ctx.kv is not the fake"
            raise TypeError(msg)
        return kv


def _http_fixtures(rows: Sequence[HttpFixtureRow]) -> list[HttpFixture]:
    return [
        HttpFixture(
            method=r.match.method,
            url_glob=r.match.url,
            status=r.response.status,
            json_body=r.response.json_,
            text=r.response.text,
            text_file=r.response.text_file,
        )
        for r in rows
    ]


def _ha_fixtures(rows: Sequence[dict[str, Any]]) -> list[HAStateFixture]:
    out: list[HAStateFixture] = []
    for r in rows:
        attrs: object = r.get("attributes") or {}
        out.append(
            HAStateFixture(
                entity_id=str(r["entity_id"]),
                uuid=str(r.get("uuid") or r["entity_id"]),
                state=str(r.get("state", "unknown")),
                attributes=dict(cast(dict[str, Any], attrs)) if isinstance(attrs, dict) else {},
                area_id=r.get("area_id"),
                device_class=r.get("device_class"),
                name=r.get("name"),
            )
        )
    return out


def profile_fixture(evals: EvalsFile | None, profile: str) -> ProfileFixture:
    """The eval's profile override, else one of the four defaults (§12.1)."""
    if evals is not None and profile in evals.fixtures.profiles:
        return evals.fixtures.profiles[profile]
    if profile in DEFAULT_PROFILES:
        return DEFAULT_PROFILES[profile]
    msg = f"unknown eval profile {profile!r}; define it under fixtures.profiles"
    raise KeyError(msg)


def fake_context(
    module_dir: Path | str,
    *,
    profile: str | None = None,
    settings: Mapping[str, Any] | None = None,
    secrets: Mapping[str, str] | None = None,
    kv: Mapping[str, Mapping[str, Any]] | None = None,
    http: Sequence[dict[str, Any]] | None = None,
    ha: Sequence[dict[str, Any]] | None = None,
    now: datetime | None = None,
    principal: Principal | None = None,
    in_tool_call: bool | None = None,
    manifest: Manifest | None = None,
    evals: EvalsFile | None = None,
) -> FakeToolContext:
    """Build a dry-run context. Every keyword overrides the matching ``fixtures.*`` block.

    ``profile=None`` yields a household-scope hook context (``principal``, ``turn_id`` and
    friends are ``None``). With a profile the context is a tool-call context for a voice
    principal of that profile unless ``in_tool_call=False``.
    """
    module_dir = Path(module_dir)
    manifest = manifest or load_manifest(module_dir)
    if evals is None and (module_dir / "evals.yaml").exists():
        evals = load_evals(module_dir)
    fixtures = evals.fixtures if evals is not None else Fixtures()
    effects: list[Effect] = []

    merged_settings = _defaults(manifest.settings) | dict(fixtures.settings) | dict(settings or {})
    secret_values = dict(fixtures.secrets) if secrets is None else dict(secrets)
    kv_fixture: Mapping[str, Mapping[str, Any]] = fixtures.kv if kv is None else kv
    household_kv = FakeKV(dict(kv_fixture.get("household", {})), effects)
    scoped_kv = (
        household_kv
        if profile is None
        else FakeKV(dict(kv_fixture.get(profile, {})), effects, household=household_kv)
    )
    http_rows = (
        _http_fixtures(fixtures.http)
        if http is None
        else _http_fixtures([HttpFixtureRow.model_validate(r) for r in http])
    )
    ha_rows = _ha_fixtures(fixtures.ha.states if ha is None else ha)

    frozen = now or datetime.fromisoformat(
        evals.freeze_time if evals is not None else "2026-11-01T09:00:00-08:00"
    )
    in_call = in_tool_call if in_tool_call is not None else profile is not None

    profile_summary: ProfileSummary | None = None
    tz = frozen.tzinfo or ZoneInfo("UTC")
    locale = "en-US"
    if profile is not None:
        pf = profile_fixture(evals, profile)
        locale = pf.locale
        tz = ZoneInfo(pf.tz)
        profile_summary = ProfileSummary(
            id=profile, name=pf.name or profile.title(), kind=pf.kind, locale=locale
        )
        if principal is None and in_call:
            principal = Principal(
                profile_id=profile,
                via="voice",
                confidence=0.95,
                margin=0.30,
                device="eval",
                scopes=ALL_SCOPES,
                tainted=False,
                turn_id="01EVALTURN",
            )

    # A capturing logger: nothing reaches stdout under `module check --json` or pytest, and
    # tests can read `ctx.log_calls` (structlog.testing.CapturingLogger).
    captured = structlog.testing.CapturingLogger()
    log = cast(
        FilteringBoundLogger,
        structlog.wrap_logger(
            captured, processors=[], wrapper_class=structlog.make_filtering_bound_logger(0)
        ).bind(module_id=manifest.id, profile_id=profile),
    )
    return FakeToolContext(
        module_id=manifest.id,
        profile_id=profile,
        profile=profile_summary,
        settings=merged_settings,
        secrets=FakeSecrets(secret_values, effects),
        ha=FakeHA(ha_rows, effects, in_tool_call=in_call),
        http=FakeHttp(
            http_rows,
            effects,
            module_dir=module_dir,
            allowed_hosts=[e.destination for e in manifest.egress],
            settings=merged_settings,
        ),
        kv=scoped_kv,
        log=log,
        locale=locale,
        tz=tz,
        now=lambda: frozen,
        dry_run=True,
        spawn=_spawn_disabled,
        announce=FakeAnnounce(effects),
        principal=principal if in_call else None,
        grants=AllowAllGrants() if in_call else None,
        turn_id=principal.turn_id if (in_call and principal is not None) else None,
        deadline=None,
        approval=None,
        effects=effects,
        log_calls=captured.calls,
    )


def _defaults(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Schema defaults for ``settings`` (MODULES §6.2: defaults ← household ← per-profile)."""
    out: dict[str, Any] = {}
    props: object = schema.get("properties", {})
    if isinstance(props, Mapping):
        for key, sub in cast(Mapping[str, Any], props).items():
            if isinstance(sub, Mapping) and "default" in sub:
                out[str(key)] = cast(Mapping[str, Any], sub)["default"]
    return out


def _spawn_disabled(coro: Any) -> Any:
    coro.close()
    msg = "ctx.spawn is not available in dry-run (MODULES §9)"
    raise RuntimeError(msg)


__all__ = ["FakeToolContext", "fake_context", "profile_fixture"]
