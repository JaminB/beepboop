"""``beepboop.testing``: the fake context behaves as MODULES §12.3 describes."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from beepboop.module import ToolError
from beepboop.testing import fake_context, load_evals, parse_evals

TEMPLATE_DIR = Path(__file__).resolve().parents[3] / "docs" / "module-template"


async def test_hook_context_has_no_caller() -> None:
    ctx = fake_context(TEMPLATE_DIR)
    assert ctx.principal is None
    assert ctx.turn_id is None
    assert ctx.profile_id is None
    assert ctx.dry_run
    assert ctx.settings["carrier"] == "auto"
    assert ctx.now() == datetime.fromisoformat("2026-11-01T09:00:00-08:00")
    with pytest.raises(Exception, match=r"no_grant|PolicyDenied|denied"):
        await ctx.ha.call_service("light", "turn_on", [])


async def test_call_context_seeds_fixtures_and_records_effects() -> None:
    ctx = fake_context(TEMPLATE_DIR, profile="alice")
    assert ctx.principal is not None
    assert ctx.principal.profile_id == "alice"
    assert ctx.locale == "en-US"
    assert str(ctx.tz) == "America/Los_Angeles"
    assert await ctx.secrets.get("api_key") == "fixture-not-a-real-key"
    parcels = await ctx.kv.get("parcels", [])
    assert len(parcels) == 3
    await ctx.kv.set("x", 1)
    await ctx.announce("hello", priority="high")
    r = await ctx.http.get("https://api.example-carrier.test/v1/track/ups/1Z999AA10123456784")
    assert r.status_code == 200
    assert r.json()["status"] == "out_for_delivery"
    await ctx.http.post("https://api.example-carrier.test/v1/anything")
    kinds = [e["kind"] for e in ctx.effects]
    assert kinds == ["kv", "announce", "http"]
    assert ctx.kv.household() is not ctx.kv


async def test_http_refuses_undeclared_host_and_missing_fixture() -> None:
    ctx = fake_context(TEMPLATE_DIR, profile="alice")
    with pytest.raises(ToolError, match="egress"):
        await ctx.http.get("https://evil.example/x")
    with pytest.raises(ToolError) as exc:
        await ctx.http.get("https://api.example-carrier.test/v1/unknown")
    assert exc.value.code == "unavailable"


async def test_overrides_win_over_fixtures() -> None:
    ctx = fake_context(
        TEMPLATE_DIR,
        profile="alice",
        secrets={},
        kv={"alice": {"parcels": []}},
        settings={"carrier": "ups"},
    )
    assert await ctx.secrets.get("api_key") is None
    assert await ctx.kv.get("parcels") == []
    assert ctx.settings["carrier"] == "ups"


def test_evals_model_defaults_and_errors() -> None:
    evals = load_evals(TEMPLATE_DIR)
    assert evals.module == "parcel"
    assert evals.case("child_denied") is not None
    with pytest.raises(ValueError, match="api"):
        parse_evals("api: 2\nmodule: x\n")
