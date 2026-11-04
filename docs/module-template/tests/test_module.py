"""Unit tests for the `parcel` worked example (docs/MODULES.md §12.4).

`fake_context` returns a dry-run ToolContext seeded from the fixtures in ../evals.yaml, so these
tests and `beepboop module check` see the same world. Pure helpers are tested without a context.
Run with plain `pytest`; no Pi, HA, mic or LLM key needed.
"""

# Pure helpers are tested directly (MODULES §12.4), so private names are imported on purpose.
# pyright: reportPrivateUsage=false
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from beepboop.module import ToolError
from beepboop.testing import fake_context

from ..module import Module, _norm, _Parcel, _replace, _speak_status

MODULE_DIR = Path(__file__).resolve().parents[1]
FROZEN = datetime.fromisoformat("2026-11-01T09:00:00-08:00")


# ---------------------------------------------------------------- handlers with the fake context


async def test_track_all_reads_fixture_status():
    ctx = fake_context(
        MODULE_DIR, profile="alice"
    )  # settings, secrets, kv, http from evals.yaml fixtures
    out = await Module().track(ctx)
    assert "out for delivery" in out["speak"]  # shoes, from the http fixture
    assert {p["label"] for p in out["parcels"]} == {"shoes", "headphones", "book"}
    assert [e for e in ctx.effects if e["kind"] == "http"] == []  # GETs are fixtures, not effects


async def test_track_by_label_filters():
    ctx = fake_context(MODULE_DIR, profile="alice")
    out = await Module().track(ctx, query="headphones")
    assert len(out["parcels"]) == 1
    assert "Thursday" in out["speak"]


async def test_add_normalises_number_and_writes_kv():
    ctx = fake_context(MODULE_DIR, profile="alice", kv={"alice": {"parcels": []}})
    out = await Module().add(ctx, tracking_number="1Z 999 AA1 0123 456 784", label="shoes")
    assert out["tracking_number"] == "1Z999AA10123456784"
    assert out["already_tracked"] is False
    assert ctx.effects[-1]["kind"] == "kv"


async def test_add_rejects_short_number():
    ctx = fake_context(MODULE_DIR, profile="alice")
    with pytest.raises(ToolError) as e:
        await Module().add(ctx, tracking_number="123")
    assert e.value.code == "invalid_args"


async def test_remove_delivered_only():
    ctx = fake_context(MODULE_DIR, profile="alice")
    out = await Module().remove(ctx, query="delivered")
    assert out["status"] == "ok"
    assert out["removed"] == ["book"]


async def test_health_without_key_is_error():
    ctx = fake_context(MODULE_DIR, profile="alice", secrets={})
    health = await Module().health(ctx)
    assert health.status == "error"


# ---------------------------------------------------------------- pure helpers


def test_norm_strips_spaces_dashes_and_uppercases():
    assert _norm("1z 999-aa1 0123") == "1Z999AA10123"


def test_replace_keeps_order_and_swaps_fresh():
    old = [_Parcel("A", "ups", "a"), _Parcel("B", "usps", "b")]
    fresh = [_Parcel("B", "usps", "b", status="delivered")]
    merged = _replace(old, fresh)
    assert [p.tracking_number for p in merged] == ["A", "B"]
    assert merged[1].status == "delivered"


def test_speak_status_single_and_many():
    one = [_Parcel("A", "ups", "shoes", status="out_for_delivery")]
    assert _speak_status(one, FROZEN) == "Shoes is out for delivery today."
    many = [_Parcel(str(i), "ups", f"p{i}", status="in_transit") for i in range(5)]
    assert _speak_status(many, FROZEN).endswith(", and 2 more.")
