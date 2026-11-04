"""``beepboop module new | check | schema | list`` (docs/MODULES.md §13, CLAUDE.md §9)."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal

import typer

from beepboop.paths import default_data_dir
from beepboop.registry.lint import LintReport, lint_module
from beepboop.registry.manifest import MODULE_ID, SCHEMA_PATH, schema_text, write_schema

#: Evaluated once per process (B008): BB_DATA_DIR, /data in the container, else ./.data.
DATA_DIR_DEFAULT = default_data_dir()

REPO_ROOT = Path(__file__).resolve().parents[3]
TEMPLATE_DIR = REPO_ROOT / "docs" / "module-template"
MODULES_DIR = REPO_ROOT / "modules"
TEMPLATE_ID = "parcel"

app = typer.Typer(
    help="Module dev kit: scaffold, lint and run evals (MODULES §13).", no_args_is_help=True
)


def _module_dir(target: str) -> Path:
    path = Path(target)
    if path.is_dir():
        return path.resolve()
    candidate = MODULES_DIR / target
    if candidate.is_dir():
        return candidate
    if target == TEMPLATE_ID or target == TEMPLATE_DIR.name:
        return TEMPLATE_DIR
    raise typer.BadParameter(f"no module folder for {target!r}")


def _substitute(text: str, new_id: str) -> str:
    title = "".join(part.title() for part in new_id.split("_"))
    return (
        text.replace(TEMPLATE_ID.title(), title)
        .replace(TEMPLATE_ID.upper(), new_id.upper())
        .replace(TEMPLATE_ID, new_id)
    )


Scope = Literal["household", "profile"]
Category = Literal[
    "home", "media", "productivity", "information", "communication", "persona", "system"
]
SCOPES: tuple[str, ...] = ("household", "profile")
CATEGORIES: tuple[str, ...] = (
    "home", "media", "productivity", "information", "communication", "persona", "system",
)  # fmt: skip

#: Where the worked example's carrier-specific choices live; each gets a TODO marker so the
#: author can grep for what to replace (MODULES §13). Applied before the id substitution.
_SUMMARY_LINE = 'summary: "Track the user\'s own parcels and say when they arrive."'
_CARRIER_LINE = "  - destination: api.example-carrier.test"
_CLIENT_LINE = "The carrier API below is fictional. Replace `_CarrierClient` with your integration."
TODO_MARKERS: dict[str, list[tuple[str, str]]] = {
    "manifest.yaml": [
        ("# This is the worked example `parcel`.", "# TODO: replace the worked example `parcel`."),
        (_SUMMARY_LINE, _SUMMARY_LINE + "   # TODO: one line, <= 20 words"),
        ("keywords: [", "keywords: [   # TODO: words people say, <= 12\n  "),
        (_CARRIER_LINE, _CARRIER_LINE + "   # TODO: your API host"),
    ],
    "module.py": [(_CLIENT_LINE, "TODO: the" + _CLIENT_LINE[3:])],
    "AGENT.md": [
        (
            "# parcel\n",
            "# parcel\n\n<!-- TODO: rewrite every section for your module (MODULES §10) -->\n",
        )
    ],
    "SETUP.md": [
        ("# ", "<!-- TODO: prerequisites, credentials and troubleshooting (MODULES §11) -->\n# ")
    ],
    "evals.yaml": [
        ("cases:", "cases:   # TODO: >= 3 cases per tool, >= 1 negative, >= 1 denial (MODULES §12)")
    ],
}


def _mark_todos(name: str, text: str) -> str:
    for old, new in TODO_MARKERS.get(name, []):
        text = text.replace(old, new, 1)
    return text


def scaffold(
    new_id: str,
    *,
    scope: str = "profile",
    category: str = "productivity",
    ha: bool = False,
    modules_dir: Path | None = None,
    template_dir: Path = TEMPLATE_DIR,
) -> Path:
    """Copy the template to ``modules/<id>``, leaving TODO markers and substituting the id
    (MODULES §13). Options are validated before anything is written."""
    modules_dir = MODULES_DIR if modules_dir is None else modules_dir
    if not MODULE_ID.match(new_id):
        raise typer.BadParameter(
            f"{new_id!r}: a module id is lowercase letters, digits and _, 2-32 characters"
        )
    if scope not in SCOPES:
        raise typer.BadParameter(f"--scope must be one of {', '.join(SCOPES)}")
    if category not in CATEGORIES:
        raise typer.BadParameter(f"--category must be one of {', '.join(CATEGORIES)}")
    dest = modules_dir / new_id
    if dest.exists():
        raise typer.BadParameter(f"{dest} already exists")
    dest.mkdir(parents=True)
    for name in ("manifest.yaml", "module.py", "AGENT.md", "SETUP.md", "evals.yaml"):
        text = _mark_todos(name, (template_dir / name).read_text(encoding="utf-8"))
        (dest / name).write_text(_substitute(text, new_id), encoding="utf-8", newline="\n")
    (dest / "tests").mkdir()
    test_text = (template_dir / "tests" / "test_module.py").read_text(encoding="utf-8")
    (dest / "tests" / "test_module.py").write_text(
        _substitute(test_text, new_id), encoding="utf-8", newline="\n"
    )
    manifest_path = dest / "manifest.yaml"
    manifest = manifest_path.read_text(encoding="utf-8")
    manifest = re.sub(r"^scope: \w+", f"scope: {scope}", manifest, count=1, flags=re.M)
    manifest = re.sub(r"^category: \w+", f"category: {category}", manifest, count=1, flags=re.M)
    if scope == "household":
        manifest = manifest.replace(
            "# profile: each person connects their own carrier account and tracking numbers",
            "# household: one connection and one set of settings for the whole house",
            1,
        )
    if ha:
        ha_block = (
            'requires:\n  core: ">=0.1,<1"\n'
            "  ha:                              # HA-backed (MODULES §5.7): TODO list the HA"
            " domains you read\n"
            "    domains: []                    # e.g. [weather]; then replace _CarrierClient"
            " with ctx.ha and drop egress"
        )
        manifest = manifest.replace('requires:\n  core: ">=0.1,<1"', ha_block, 1)
    manifest_path.write_text(manifest, encoding="utf-8", newline="\n")
    return dest


def print_report(report: LintReport, *, as_json: bool = False) -> None:
    """Findings one per line, prefixed with the module folder so a multi-module run reads."""
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "module": report.module_dir.name,
                    "ok": report.ok,
                    "findings": [asdict(f) for f in report.findings],
                },
                indent=2,
            )
        )
        return
    for f in report.findings:
        typer.echo(f"{report.module_dir.name}: {f}")
    typer.echo(
        f"{report.module_dir.name}: {'ok' if report.ok else 'FAILED'} ({len(report.errors)}"
        f" errors, {len(report.warnings)}"
        f" warnings)"
    )


@app.command("new")
def new(
    module_id: Annotated[
        str, typer.Argument(help="lowercase letters, digits and _; 2-32 characters")
    ],
    ha: Annotated[bool, typer.Option("--ha", help="Scaffold an HA-backed module (§5.7).")] = False,
    scope: Annotated[Scope, typer.Option(help="household | profile")] = "profile",
    category: Annotated[Category, typer.Option(help="Module category (§4.1).")] = "productivity",
) -> None:
    """Scaffold modules/<id>/ from docs/module-template/ and run `module check` once."""
    from beepboop.testing.evals import run_evals

    dest = scaffold(module_id, scope=scope, category=category, ha=ha)
    typer.echo(f"created {dest}")
    report = lint_module(dest)
    print_report(report)
    evals_ok = False
    if report.ok:
        evals = run_evals(dest)
        evals_ok = evals.ok
        typer.echo(
            f"{dest.name}: evals {'ok' if evals.ok else 'FAILED'} ({len(evals.results)} cases)"
        )
    rel = dest.relative_to(Path.cwd()) if dest.is_relative_to(Path.cwd()) else dest
    typer.echo("next steps:")
    typer.echo(f"  1. grep -n TODO {rel}      # every carrier-specific choice is marked")
    typer.echo(f"  2. edit {rel}/manifest.yaml, module.py, AGENT.md, SETUP.md, evals.yaml")
    typer.echo(f"  3. uv run beepboop module check {rel}   # lint L1-L18 + offline evals")
    typer.echo(f"  4. uv run pytest {rel}")
    typer.echo("  5. uv run beepboop chat --seed --as alex --dry-run --trace   # drive it")
    raise typer.Exit(code=0 if report.ok and evals_ok else 1)


@app.command("check")
def check(
    target: Annotated[str | None, typer.Argument(help="<id> | <path>; omit with --all")] = None,
    all_: Annotated[bool, typer.Option("--all", help="Every module under modules/.")] = False,
    llm: Annotated[
        bool, typer.Option("--llm", help="Run evals through the real agent loop.")
    ] = False,
    show_card: Annotated[bool, typer.Option("--show-card")] = False,
    rubric: Annotated[bool, typer.Option("--rubric")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Lint L1-L18, token budgets, examples and offline evals. Exit 1 on any error."""
    from beepboop.registry.find_tools import module_card
    from beepboop.registry.rubric import auto_rubric
    from beepboop.testing.evals import run_evals

    if llm:
        typer.echo(
            "module check --llm needs a configured LLM provider; not wired yet (agent builder).",
            err=True,
        )
        raise typer.Exit(code=2)
    if all_:
        dirs = (
            sorted(p for p in MODULES_DIR.iterdir() if p.is_dir()) if MODULES_DIR.exists() else []
        )
    elif target is not None:
        dirs = [_module_dir(target)]
    else:
        raise typer.BadParameter("give a module id/path or --all")
    failed = False
    documents: list[dict[str, object]] = []
    for module_dir in dirs:
        report = lint_module(module_dir)
        doc: dict[str, object] = {
            "module": module_dir.name,
            "lint_ok": report.ok,
            "findings": [asdict(f) for f in report.findings],
        }
        if not as_json:
            print_report(report)
            if report.manifest is not None and show_card:
                typer.echo(module_card(report.manifest))
        if report.ok:
            evals = run_evals(module_dir)
            doc["evals_ok"] = evals.ok
            if not as_json and report.manifest is not None and rubric:
                typer.echo(auto_rubric(module_dir, report.manifest, evals_ok=evals.ok))
            doc["evals"] = [
                {"id": r.case_id, "ok": r.ok, "messages": list(r.messages)} for r in evals.results
            ]
            if not as_json:
                for r in evals.failed:
                    typer.echo(f"eval {r.case_id}: " + "; ".join(r.messages))
                for e in evals.errors:
                    typer.echo(f"evals: {e}")
                typer.echo(
                    f"{module_dir.name}: evals {'ok' if evals.ok else 'FAILED'}"
                    f" ({len(evals.results)} cases)"
                )
            failed = failed or not evals.ok
        failed = failed or not report.ok
        doc["ok"] = report.ok and bool(doc.get("evals_ok", False))
        documents.append(doc)
    if as_json:
        typer.echo(json.dumps(documents if len(documents) != 1 else documents[0], indent=2))
    raise typer.Exit(code=1 if failed else 0)


@app.command("schema")
def schema(
    check_only: Annotated[
        bool, typer.Option("--check", help="Fail if the checked-in schema is stale.")
    ] = False,
) -> None:
    """Write (or verify) core/beepboop/registry/manifest.schema.json from the model."""
    if check_only:
        current = SCHEMA_PATH.read_text(encoding="utf-8") if SCHEMA_PATH.exists() else ""
        if current != schema_text():
            typer.echo(f"{SCHEMA_PATH} is stale; run `beepboop module schema`", err=True)
            raise typer.Exit(code=1)
        typer.echo("manifest.schema.json is up to date")
        return
    typer.echo(f"wrote {write_schema()}")


@app.command("list")
def list_(
    profile: Annotated[
        str | None,
        typer.Option("--profile", help="Also show which tools this profile (id or name) can call."),
    ] = None,
    data_dir: Annotated[Path, typer.Option("--data-dir", envvar="BB_DATA_DIR")] = DATA_DIR_DEFAULT,
) -> None:
    """Modules under modules/: id, backed by, scope, version, tool count, health."""
    from beepboop.registry.loader import discover

    callable_tools: set[str] | None = None
    if profile is not None:
        callable_tools = asyncio.run(_callable_for(profile, data_dir))
    typer.echo(f"{'module':16} {'backed':6} {'scope':9} {'version':8} {'tools':>5}  state")
    for found in discover(MODULES_DIR):
        m = found.manifest
        backed = "HA" if m.ha_backed else "native"
        state = f"error: {found.error}" if found.error else "ok"
        n = len(m.tools)
        typer.echo(f"{m.id:16} {backed:6} {m.scope:9} {m.version:8} {n:>5}  {state}")
        if callable_tools is not None:
            mine = sorted(t.name for t in m.tools if t.name in callable_tools)
            typer.echo(f"{'':16} {profile} can call: {', '.join(mine) if mine else 'nothing'}")


async def _callable_for(profile: str, data_dir: Path) -> set[str]:
    from beepboop.db import repo
    from beepboop.policy.types import ALL_SCOPES, Principal
    from beepboop.service import CoreConfig, CoreService

    base = CoreConfig.from_env()
    service = await CoreService.start(
        CoreConfig(
            data_dir=data_dir,
            modules_dir=base.modules_dir,
            locale=base.locale,
            timezone=base.timezone,
            kek_passphrase_file=base.kek_passphrase_file,
        )
    )
    try:
        row = await service.db.read(
            lambda c: repo.profiles.get(c, profile) or repo.profiles.by_name(c, profile)
        )
        if row is None:
            raise typer.BadParameter(f"no profile {profile!r}")
        p = Principal(row.id, "web", None, None, "cli", ALL_SCOPES, False, "cli")
        return {t.name for t in service.registry.callable_by(p)}
    finally:
        await service.stop()


def remove_scaffold(path: Path) -> None:  # pragma: no cover - test helper
    shutil.rmtree(path)


__all__ = ["MODULES_DIR", "TEMPLATE_DIR", "app", "print_report", "scaffold"]
