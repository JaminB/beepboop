"""``beepboop chat --as <profile> [--dry-run] [--trace] [--seed] [--fake-llm]``
(CLAUDE.md §9, MODULES §13).

Text chat through the real policy path, no mic: the same ``CoreService`` wiring as
``beepboop serve`` (database, registry, modules, ladder), one ``TurnLoop.handle`` per
line. The CLI is a web-door principal (IDENTITY_AND_TRUST §3.1): ``via="web"``, no voice
confidence. ``--dry-run`` records intended effects instead of acting; ``--trace`` prints
the "why did you do that" explanation of each turn. A ``critical`` call prints the
pending approval id; approve it in the web UI.

Dev loop helpers: ``--seed`` creates the three dev people (Alex the admin, Sam the member,
Robin the child) when they do not exist, so ``--as alex`` works on a fresh checkout;
``--fake-llm`` answers deferred-module requests from the modules' ``evals.yaml``
expectations instead of a model, so a module can be driven offline.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import sys
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Annotated

import typer

from beepboop.agent.phrases import PHRASES
from beepboop.agent.types import Speech
from beepboop.paths import default_data_dir

#: Evaluated once per process (B008): BB_DATA_DIR, /data in the container, else ./.data.
DATA_DIR_DEFAULT = default_data_dir()

#: ``--seed``: (name, preset); the same household the dev server seeds.
SEED_PEOPLE: tuple[tuple[str, str], ...] = (
    ("Alex", "admin"),
    ("Sam", "member"),
    ("Robin", "child"),
)
PROMPT = "you> "


class PrintSpeaker:
    """``Speaker`` for the terminal: clips print their §11.6 text."""

    async def speak(self, turn_id: str, speech: Speech, *, last: bool) -> None:
        line = speech.text or PHRASES.get(speech.clip or "", f"[{speech.clip}]")
        typer.echo(f"beepboop> {line}")

    async def earcon(self, turn_id: str, name: str) -> None:
        typer.echo(f"  ({name})")


async def stdin_lines(prompt: str = PROMPT) -> AsyncIterator[str]:
    """Read the terminal without blocking the event loop, so timer rings and announcements
    still arrive while the REPL waits for input."""
    interactive = sys.stdin.isatty()
    while True:
        try:
            line = await asyncio.to_thread(input, prompt if interactive else "")
        except EOFError:
            return
        yield line


def seed_people(conn: sqlite3.Connection) -> list[str]:
    """Create the dev household (no logins) for the names that do not exist yet."""
    from beepboop.api.people import CreatablePreset, create_profile
    from beepboop.db import repo

    made: list[str] = []
    for name, preset in SEED_PEOPLE:
        if repo.profiles.by_name(conn, name) is None:
            kind: CreatablePreset = (
                "admin" if preset == "admin" else "child" if preset == "child" else "member"
            )
            create_profile(conn, name, kind, created_by=None)
            made.append(name)
    return made


async def run_chat(
    profile: str,
    lines: Iterable[str] | AsyncIterator[str],
    *,
    data_dir: Path,
    modules_dir: Path | None,
    dry_run: bool,
    trace: bool,
    seed: bool = False,
    fake_llm: bool = False,
) -> int:
    from beepboop.agent.records import explain_turn
    from beepboop.agent.types import Utterance
    from beepboop.db import repo
    from beepboop.policy.types import ALL_SCOPES, Principal
    from beepboop.service import CoreConfig, CoreService
    from beepboop.testing.llm import EvalsProvider
    from beepboop_protocol import new_ulid

    base = CoreConfig.from_env()
    config = CoreConfig(
        data_dir=data_dir,
        modules_dir=modules_dir or base.modules_dir,
        locale=base.locale,
        timezone=base.timezone,
        kek_passphrase_file=base.kek_passphrase_file,
    )
    service = await CoreService.start(config, speaker=PrintSpeaker())
    try:
        if seed:
            made = await service.db.write(seed_people, durable=True)
            await service.refresh_policy()
            if made:
                typer.echo(f"seeded {', '.join(made)} (dev household; no logins)")
        row = await service.db.read(
            lambda c: repo.profiles.get(c, profile) or repo.profiles.by_name(c, profile)
        )
        if row is None:
            typer.echo(
                f"no profile {profile!r}. Create one in the web UI (People > Add), or run"
                " `beepboop chat --seed --as alex` for the dev household (Alex, Sam, Robin).",
                err=True,
            )
            return 2
        names = {row.id: row.name}
        loop = service.loop
        loop.dry_run = dry_run
        if fake_llm:
            loop.provider = EvalsProvider.from_modules(config.modules_dir)
            typer.echo("fake LLM: tool choice comes from the modules' evals.yaml, not a model")

        async def _each() -> AsyncIterator[str]:
            if isinstance(lines, AsyncIterator):
                async for raw in lines:
                    yield raw
            else:
                for raw in lines:
                    yield raw

        async for raw in _each():
            text = raw.strip()
            if not text:
                continue
            p = Principal(row.id, "web", None, None, "cli", ALL_SCOPES, False, new_ulid())
            result = await loop.handle(Utterance(text, p, device="cli"))
            if result.pending is not None:
                typer.echo(
                    f"  pending approval {result.pending.approval_id}: approve in the web UI"
                )
            if trace:
                turn_id = p.turn_id
                ex = await service.db.read(lambda c, tid=turn_id: repo.turns.explain(c, tid))
                if ex is not None:
                    typer.echo(f"  why: {explain_turn(ex, names=names)}")
        return 0
    finally:
        await service.stop()


def chat(
    as_: Annotated[str, typer.Option("--as", help="Profile name or id to chat as.")],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Record effects; change nothing.")
    ] = False,
    trace: Annotated[bool, typer.Option("--trace", help="Explain each turn.")] = False,
    message: Annotated[
        list[str] | None, typer.Option("-m", "--message", help="One line; repeat for more.")
    ] = None,
    seed: Annotated[
        bool,
        typer.Option("--seed", help="Create the dev household (Alex, Sam, Robin) if absent."),
    ] = False,
    fake_llm: Annotated[
        bool,
        typer.Option(
            "--fake-llm", help="Pick tools from the modules' evals.yaml instead of a model."
        ),
    ] = False,
    data_dir: Annotated[Path, typer.Option("--data-dir", envvar="BB_DATA_DIR")] = DATA_DIR_DEFAULT,
    modules_dir: Annotated[
        Path | None, typer.Option("--modules-dir", envvar="BB_MODULES_DIR")
    ] = None,
) -> None:
    """Text chat through the real policy path, no mic (the primary dev loop)."""
    from beepboop.log import configure_logging

    # Replies are the output; structlog lines stay out of the way unless --trace asks.
    configure_logging(logging.INFO if trace else logging.WARNING, stream=sys.stderr)
    lines: Iterable[str] | AsyncIterator[str] = message if message else stdin_lines()
    code = asyncio.run(
        run_chat(
            as_,
            lines,
            data_dir=data_dir,
            modules_dir=modules_dir,
            dry_run=dry_run,
            trace=trace,
            seed=seed,
            fake_llm=fake_llm,
        )
    )
    raise typer.Exit(code=code)


__all__ = ["SEED_PEOPLE", "PrintSpeaker", "chat", "run_chat", "seed_people", "stdin_lines"]
