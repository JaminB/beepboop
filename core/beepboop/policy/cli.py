"""``beepboop policy explain --as <profile> <tool> [args]`` (IDENTITY_AND_TRUST §7.2).

The decision is computed the way a real call is: the same ``CoreService`` wiring as
``beepboop chat`` (modules loaded, HA entity index from the database, saved grants), the
registered tool's declared tier (``--risk`` only for a tool that is not registered), the
HA target extractor for ``home.*`` / ``media.control`` and then ``policy.authorize``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any, cast

import typer

from beepboop.paths import default_data_dir
from beepboop.policy.types import ALL_SCOPES, Principal, Risk, SimpleTool, Via, is_risk

#: Evaluated once per process (B008): BB_DATA_DIR, /data in the container, else ./.data.
DATA_DIR_DEFAULT = default_data_dir()

app = typer.Typer(help="Inspect policy decisions.", no_args_is_help=True)

VIAS: tuple[Via, ...] = ("web", "voice", "pat", "oauth")


async def explain_call(
    *,
    data_dir: Path,
    modules_dir: Path | None,
    profile: str,
    tool_name: str,
    args: dict[str, Any],
    risk: Risk,
    via: Via,
    confidence: float | None,
    margin: float | None,
    tainted: bool,
) -> list[str]:
    """Lines for the terminal; raises ``typer.Exit`` for an unknown profile."""
    from beepboop.db import repo
    from beepboop.policy.authorize import authorize
    from beepboop.policy.grants import effective_grants
    from beepboop.policy.types import ToolLike
    from beepboop.registry.registry import ToolNotFoundError
    from beepboop.service import CoreConfig, CoreService

    base = CoreConfig.from_env()
    config = CoreConfig(
        data_dir=data_dir,
        modules_dir=modules_dir or base.modules_dir,
        locale=base.locale,
        timezone=base.timezone,
        kek_passphrase_file=base.kek_passphrase_file,
    )
    service = await CoreService.start(config)
    try:
        row = await service.db.read(
            lambda c: repo.profiles.get(c, profile) or repo.profiles.by_name(c, profile)
        )
        if row is None:
            typer.echo(
                f"no profile {profile!r}; create one in the web UI or with `beepboop chat --seed`",
                err=True,
            )
            raise typer.Exit(code=1)
        registry = service.registry
        lines: list[str] = []
        tool: ToolLike
        try:
            registered = registry.get(tool_name)
            tool = registered
            lines.append(f"tool: {tool_name} (module {registered.module_id}, declared {tool.risk})")
        except ToolNotFoundError:
            tool = SimpleTool(tool_name, risk)
            lines.append(f"tool: {tool_name} (not registered; assuming --risk {risk})")
        conf = confidence if via == "voice" else None
        marg = margin if via == "voice" else None
        p = Principal(row.id, via, conf, marg, "cli", ALL_SCOPES, tainted, "cli")
        targets = []
        try:
            registered = registry.get(tool_name)
            targets = await registry.targets(p, registered, args)
        except ToolNotFoundError:
            pass
        state = service.policy_state()
        now = service.loop.clock()
        d = authorize(state, p, tool, args, targets, now)
        for t in targets:
            lines.append(
                f"target: {t.uuid} {t.domain}.{t.service or '-'}"
                f" device_class={t.device_class} declared_risk={t.declared_risk}"
            )
        lines.append(
            f"decision: {d.outcome}  risk: {d.effective_risk}  reason: {d.reason}  factors:"
            f" {list(d.factors)}"
        )
        view = state.profiles[row.id]
        for g in effective_grants(
            state.grants, row.id, view.group_ids, d.effective_risk, now, state.tz
        ):
            mark = "*" if g.id == d.grant_id else " "
            lines.append(
                f" {mark} {g.id} {g.subject} {g.resource} max_risk={g.max_risk} source={g.source}"
            )
        return lines
    finally:
        await service.stop()


@app.command()
def explain(
    tool: Annotated[str, typer.Argument(help="Tool name, e.g. home.turn_on")],
    args: Annotated[str, typer.Argument(help="JSON object of args")] = "{}",
    as_: Annotated[str, typer.Option("--as", help="Profile id or name")] = "guest",
    via: Annotated[str, typer.Option(help="web | voice | pat | oauth")] = "web",
    confidence: Annotated[float | None, typer.Option(help="Speaker score (voice only).")] = 0.95,
    margin: Annotated[float | None, typer.Option(help="Speaker margin (voice only).")] = 0.3,
    tainted: Annotated[bool, typer.Option("--tainted", help="Conversation is tainted.")] = False,
    risk: Annotated[
        str, typer.Option(help="Declared tier when the tool is not registered")
    ] = "act",
    data_dir: Annotated[Path, typer.Option("--data-dir", envvar="BB_DATA_DIR")] = DATA_DIR_DEFAULT,
    modules_dir: Annotated[
        Path | None, typer.Option("--modules-dir", envvar="BB_MODULES_DIR")
    ] = None,
) -> None:
    """Print the authorize decision, the HA targets and the grants that produced it."""
    if not is_risk(risk):
        raise typer.BadParameter("risk must be read|act|personal|critical")
    if via not in VIAS:
        raise typer.BadParameter("via must be web|voice|pat|oauth")
    parsed: dict[str, Any] = cast(dict[str, Any], json.loads(args))
    lines = asyncio.run(
        explain_call(
            data_dir=data_dir,
            modules_dir=modules_dir,
            profile=as_,
            tool_name=tool,
            args=parsed,
            risk=cast(Risk, risk),
            via=via,
            confidence=confidence,
            margin=margin,
            tainted=tainted,
        )
    )
    for line in lines:
        typer.echo(line)


__all__ = ["app", "explain_call"]
