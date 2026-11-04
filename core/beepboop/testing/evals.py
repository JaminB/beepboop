"""The offline evals runner behind ``beepboop module check`` (docs/MODULES.md §12.2).

For every case: the **policy decision** for the given preset profile (grants, ceiling,
risk, taint) and, when ``expect.tool`` names one of this module's tools and the args carry
no matchers, the handler in dry-run with the fixtures; then effects and ``speak`` compared.
``ask_user`` and ``path`` are LLM-mode assertions and only validated for shape here.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import jsonschema

from beepboop.module.base import Module, collect_tools
from beepboop.module.types import ToolError, ToolResult
from beepboop.policy import (
    ALL_SCOPES,
    EntityInfo,
    Grant,
    GrantIndex,
    PolicyState,
    Principal,
    ProfileView,
    Target,
    apply_preset,
    authorize,
)
from beepboop.registry.manifest import Manifest, ToolSpec, load_manifest
from beepboop.registry.resolver import Area, StaticEntityIndex
from beepboop.testing.context import fake_context, profile_fixture
from beepboop.testing.evals_model import EvalCase, EvalsFile, load_evals
from beepboop.testing.fakes import HAStateFixture


@dataclass(frozen=True, slots=True)
class CaseResult:
    case_id: str
    ok: bool
    messages: tuple[str, ...] = ()
    decision: str | None = None
    ran_handler: bool = False


@dataclass(frozen=True, slots=True)
class EvalsReport:
    module_id: str
    results: tuple[CaseResult, ...]
    errors: tuple[str, ...] = field(default=())

    @property
    def ok(self) -> bool:
        return not self.errors and all(r.ok for r in self.results)

    @property
    def failed(self) -> tuple[CaseResult, ...]:
        return tuple(r for r in self.results if not r.ok)


_MATCHER_KEYS = frozenset({"$regex", "$any"})


def has_matchers(args: Mapping[str, Any]) -> bool:
    return any(
        isinstance(v, dict) and bool(_MATCHER_KEYS & set(cast(dict[str, Any], v)))
        for v in args.values()
    )


def value_matches(expected: Any, actual: Any) -> bool:
    """``expect.args`` / ``expect.effects`` value match: exact, ``{$regex}`` or ``{$any}``."""
    if isinstance(expected, dict):
        exp = cast(dict[str, Any], expected)
        if "$any" in exp:
            return True
        if "$regex" in exp:
            return isinstance(actual, str) and re.search(str(exp["$regex"]), actual) is not None
        if not isinstance(actual, dict):
            return False
        got = cast(dict[str, Any], actual)
        return all(k in got and value_matches(v, got[k]) for k, v in exp.items())
    return bool(expected == actual)


def subset_match(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    return all(k in actual and value_matches(v, actual[k]) for k, v in expected.items())


def effects_subset(expected: list[dict[str, Any]], actual: list[dict[str, Any]]) -> bool:
    """Every expected effect matches at least one recorded effect, in order."""
    pos = 0
    for exp in expected:
        while pos < len(actual) and not subset_match(exp, actual[pos]):
            pos += 1
        if pos >= len(actual):
            return False
        pos += 1
    return True


def policy_state_for(manifest: Manifest, evals: EvalsFile) -> PolicyState:
    """The four preset profiles plus this module's tools granted at declared risk where the
    preset allows (§12.1): a module tool is raised to its declared risk only for a profile
    whose preset (or ``fixtures.profiles.<name>.grants`` override) already names it with a
    ``tool_glob``, so a Guest is denied ``list.add`` here exactly as in production. The
    ceiling still applies. HA fixture entities are granted to every profile."""
    profiles: dict[str, ProfileView] = {}
    grants: list[Grant] = []
    counter = iter(range(10_000))
    names = set(evals.fixtures.profiles) | {"admin", "member", "child", "guest"}
    for name in sorted(names):
        pf = profile_fixture(evals, name)
        rows = apply_preset(pf.kind, name, lambda: f"g{next(counter)}")
        profiles[name] = ProfileView(
            id=name,
            kind=pf.kind,
            risk_ceiling=rows.risk_ceiling,
            group_ids=frozenset(rows.group_ids),
            has_login=pf.kind in ("admin", "member"),
        )
        own = list(rows.grants)
        if pf.grants is not None:
            own = [
                Grant(
                    f"g{next(counter)}",
                    "profile",
                    name,
                    g.resource_type,
                    g.resource_id,
                    g.max_risk,
                    source="eval",
                )
                for g in pf.grants
            ]
        grants.extend(own)
        for tool in manifest.tools:
            if not any(g.covers_tool(tool.name) for g in own):
                continue
            grants.append(
                Grant(
                    f"g{next(counter)}",
                    "profile",
                    name,
                    "tool_glob",
                    tool.name,
                    tool.risk,
                    source="eval",
                )
            )
    entities: dict[str, EntityInfo] = {}
    for row in evals.fixtures.ha.states:
        uuid = str(row.get("uuid") or row["entity_id"])
        entities[uuid] = EntityInfo(uuid, "active", area_id=row.get("area_id"))
        for name in sorted(names):
            grants.append(
                Grant(
                    f"g{next(counter)}",
                    "profile",
                    name,
                    "ha_entity",
                    uuid,
                    "critical",
                    source="eval",
                )
            )
    return PolicyState(profiles=profiles, grants=GrantIndex.build(grants), entities=entities)


def fixture_targets(
    evals: EvalsFile, tool: str, args: Mapping[str, Any], principal: Principal
) -> list[Target]:
    """For ``home.*`` / ``media.control``: the policy targets the real extractor would
    produce over ``fixtures.ha.states`` (same resolver, same tool → service table), so a
    child unlocking a lock is denied by ceiling and an adult gets ``confirm``, offline."""
    from beepboop.ha.plan import PLANNED_TOOLS, plan_entities, service_for

    if tool not in PLANNED_TOOLS or not evals.fixtures.ha.states:
        return []
    rows = [
        HAStateFixture(
            entity_id=str(r["entity_id"]),
            uuid=str(r.get("uuid") or r["entity_id"]),
            state=str(r.get("state", "unknown")),
            attributes=dict(r.get("attributes") or {}),
            area_id=r.get("area_id"),
            device_class=r.get("device_class"),
            name=r.get("name"),
        )
        for r in evals.fixtures.ha.states
    ]
    refs = [r.ref() for r in rows]
    areas = sorted({r.area_id for r in refs if r.area_id})
    index = StaticEntityIndex(
        refs,
        [Area(a, a.replace("_", " ")) for a in areas],
        states={r.uuid: r.state for r in rows},
    )
    _, entities = plan_entities(tool, args, principal, index)
    return [
        Target(e.uuid, e.domain, service_for(tool, e.domain, args), e.device_class)
        for e in entities
    ]


def _principal(case: EvalCase) -> Principal:
    v = case.voice
    return Principal(
        case.profile, "voice", v.confidence, v.margin, "eval", ALL_SCOPES, False, "01EVALTURN"
    )


async def _run_case(
    module_dir: Path,
    manifest: Manifest,
    evals: EvalsFile,
    state: PolicyState,
    module: Module,
    case: EvalCase,
    now: datetime,
) -> CaseResult:
    msgs: list[str] = []
    exp = case.expect
    tool_name = exp.tool
    if tool_name is None and (exp.denied or exp.confirm):
        # No tool named: the policy outcome must hold for every tool of this module.
        try:
            profile_fixture(evals, case.profile)
        except KeyError as e:
            return CaseResult(case.id, False, (str(e),))
        wanted = "deny" if exp.denied else "confirm"
        for spec in manifest.tools:
            d = authorize(state, _principal(case), spec, {}, [], now)
            if d.outcome != wanted:
                msgs.append(f"{spec.name}: expected {wanted}, policy said {d.outcome} ({d.reason})")
        return CaseResult(case.id, not msgs, tuple(msgs), wanted)
    if tool_name is None:
        # A negative case: none of this module's tools. Nothing to run offline.
        if exp.args is not None:
            msgs.append("tool: null cannot combine with args")
        return CaseResult(case.id, not msgs, tuple(msgs))

    spec: ToolSpec | None = manifest.tool(tool_name)
    if spec is None:
        if tool_name.split(".", 1)[0] == manifest.id:
            msgs.append(f"unknown tool {tool_name!r}")
        return CaseResult(case.id, not msgs, tuple(msgs))

    args = dict(exp.args or {})
    try:
        profile_fixture(evals, case.profile)
    except KeyError as e:
        return CaseResult(case.id, False, (str(e),))

    principal = _principal(case)
    targets = [] if has_matchers(args) else fixture_targets(evals, spec.name, args, principal)
    decision = authorize(state, principal, spec, args, targets, now)
    if exp.denied and decision.outcome != "deny":
        msgs.append(f"expected a denial, policy said {decision.outcome}")
    if exp.confirm and decision.outcome != "confirm":
        msgs.append(f"expected confirm, policy said {decision.outcome} ({decision.reason})")
    if not exp.denied and not exp.confirm and decision.outcome == "deny":
        msgs.append(f"policy denied ({decision.reason}) but the case did not expect it")
    if exp.taint and not spec.untrusted_output:
        msgs.append("taint: true but the tool is not untrusted_output")

    ran = False
    if decision.outcome == "allow" and not has_matchers(args) and exp.args is not None:
        try:
            jsonschema.validate(args, spec.args)
        except jsonschema.ValidationError as e:
            msgs.append(f"expect.args do not validate against the tool schema: {e.message}")
            return CaseResult(case.id, False, tuple(msgs), decision.outcome)
        ctx = fake_context(module_dir, profile=case.profile, manifest=manifest, evals=evals)
        handler = collect_tools(module).get(tool_name)
        if handler is None:
            msgs.append(f"no @tool handler for {tool_name}")
        else:
            try:
                out = await handler(ctx, **args)
                ran = True
            except ToolError as e:
                msgs.append(f"handler raised ToolError({e.code}): {e.message}")
                return CaseResult(case.id, False, tuple(msgs), decision.outcome)
            except Exception as e:  # MODULES §5.2: any other exception is tool_failed
                tb = e.__traceback__
                while tb is not None and tb.tb_next is not None:
                    tb = tb.tb_next
                where = (
                    f" at {Path(tb.tb_frame.f_code.co_filename).name}:{tb.tb_lineno}"
                    if tb is not None
                    else ""
                )
                msgs.append(f"handler raised {type(e).__name__}{where}: {e}")
                return CaseResult(case.id, False, tuple(msgs), decision.outcome)
            structured = out.structured if isinstance(out, ToolResult) else out
            # §5.2 / §4.5: the result must match ``output`` and carry a spoken line.
            try:
                jsonschema.validate(structured, spec.output)
            except jsonschema.ValidationError as e:
                msgs.append(f"result does not match the tool's output schema: {e.message}")
            spoken = structured.get("speak")
            if not isinstance(spoken, str) or not spoken.strip():
                msgs.append("result has no non-empty 'speak' string")
            speak = str(structured.get("speak", ""))
            for needle in exp.speak_contains:
                if needle.lower() not in speak.lower():
                    msgs.append(f"speak {speak!r} does not contain {needle!r}")
            if exp.effects is not None and not effects_subset(exp.effects, ctx.effects):
                msgs.append(f"effects {ctx.effects!r} do not include {exp.effects!r}")
    return CaseResult(case.id, not msgs, tuple(msgs), decision.outcome, ran)


def load_module_class(module_dir: Path) -> type[Module]:
    """Import ``<module_dir>/module.py`` in isolation and return its ``Module`` class."""
    from beepboop.registry.loader import import_module_file

    return import_module_file(module_dir)


async def run_evals_async(module_dir: Path, *, module: Module | None = None) -> EvalsReport:
    module_dir = Path(module_dir)
    manifest = load_manifest(module_dir)
    try:
        evals = load_evals(module_dir)
    except ValueError as e:
        return EvalsReport(manifest.id, (), (str(e),))
    if evals.module != manifest.id:
        return EvalsReport(manifest.id, (), (f"evals.module {evals.module!r} != {manifest.id!r}",))
    instance = module or load_module_class(module_dir)()
    state = policy_state_for(manifest, evals)
    now = datetime.fromisoformat(evals.freeze_time)
    results = [
        await _run_case(module_dir, manifest, evals, state, instance, case, now)
        for case in evals.cases
    ]
    return EvalsReport(manifest.id, tuple(results))


def run_evals(module_dir: Path, *, module: Module | None = None) -> EvalsReport:
    """Synchronous wrapper for the CLI and tests."""
    return asyncio.run(run_evals_async(module_dir, module=module))


__all__ = [
    "CaseResult",
    "EvalsReport",
    "effects_subset",
    "fixture_targets",
    "has_matchers",
    "policy_state_for",
    "run_evals",
    "run_evals_async",
    "subset_match",
    "value_matches",
]
