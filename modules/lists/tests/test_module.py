"""Unit tests for the `lists` module (docs/MODULES.md §12.4).

`fake_context` is a dry-run ToolContext seeded from ../evals.yaml: a shared shopping list
(milk, eggs) and Alice's personal to-do list.
"""

# Pure helpers are tested directly (MODULES §12.4), so private names are imported on purpose.
# pyright: reportPrivateUsage=false
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from beepboop.module import ToolError
from beepboop.testing import fake_context

from ..module import (
    KEY_PREFIX,
    MAX_ITEMS,
    Module,
    _List,
    canonical_name,
    item_key,
    merge,
    read_text,
    take,
)

MODULE_DIR = Path(__file__).resolve().parents[1]
NOW = datetime.fromisoformat("2026-11-01T09:00:00-08:00")


async def test_add_to_shared_list_writes_household_scope() -> None:
    ctx = fake_context(MODULE_DIR, profile="alice")
    out = await Module().add(ctx, items=["Bread", "butter."])
    assert out["added"] == ["bread", "butter"]
    assert out["count"] == 4
    stored = await ctx.kv.household().get(KEY_PREFIX + "shopping")
    assert [i["text"] for i in stored["items"]] == ["milk", "eggs", "bread", "butter"]
    assert await ctx.kv.get(KEY_PREFIX + "shopping") is None  # not in Alice's scope


async def test_add_duplicate_is_a_no_op() -> None:
    ctx = fake_context(MODULE_DIR, profile="alice")
    out = await Module().add(ctx, items=["Eggs", "egg", "jam"])
    assert out["added"] == ["jam"]
    assert out["already"] == ["eggs", "egg"]
    assert out["speak"] == "Added jam to the shopping list. Eggs and egg are already on it."


async def test_add_personal_creates_list_in_profile_scope() -> None:
    ctx = fake_context(MODULE_DIR, profile="alice")
    out = await Module().add(ctx, items=["socks"], list="My Packing List", personal=True)
    assert out["list"] == "packing"
    assert out["speak"] == "Added socks to your packing list."
    assert (await ctx.kv.get(KEY_PREFIX + "packing"))["items"][0]["text"] == "socks"


async def test_add_rejects_overfull_list_and_too_many_lists() -> None:
    full = {"name": "shopping", "items": [{"text": f"thing {i}"} for i in range(MAX_ITEMS)]}
    ctx = fake_context(MODULE_DIR, profile="alice", kv={"household": {"list:shopping": full}})
    with pytest.raises(ToolError) as e:
        await Module().add(ctx, items=["one more"])
    assert e.value.code == "invalid_args"
    many: dict[str, object] = {f"list:l{i}": {"name": f"l{i}", "items": []} for i in range(30)}
    ctx = fake_context(MODULE_DIR, profile="alice", kv={"household": many})
    with pytest.raises(ToolError):
        await Module().add(ctx, items=["x"], list="new")


async def test_personal_list_needs_a_profile() -> None:
    ctx = fake_context(MODULE_DIR)
    with pytest.raises(ToolError):
        await Module().read(ctx, list="to-do", personal=True)


async def test_read_shared_and_personal() -> None:
    ctx = fake_context(MODULE_DIR, profile="alice")
    shared = await Module().read(ctx)
    assert shared == {
        "speak": "The shopping list has 2 items: milk and eggs.",
        "list": "shopping",
        "items": ["milk", "eggs"],
    }
    mine = await Module().read(ctx, list="todo", personal=True)
    assert mine["speak"] == "Your to-do list has 1 item: renew passport."
    assert ctx.effects == []


async def test_another_profile_cannot_see_alices_list() -> None:
    ctx = fake_context(MODULE_DIR, profile="member")
    out = await Module().read(ctx, list="to-do", personal=True)
    assert out["items"] == []
    assert out["speak"] == "You don't have a to-do list."


async def test_remove_items_and_clear() -> None:
    ctx = fake_context(MODULE_DIR, profile="alice")
    out = await Module().remove(ctx, items=["the Eggs", "kiwis"])
    assert out["removed"] == ["eggs"]
    assert out["speak"] == "Removed eggs from the shopping list. Kiwis isn't on the shopping list."
    cleared = await Module().remove(ctx, clear=True)
    assert cleared["removed"] == ["milk"]
    assert await ctx.kv.household().get(KEY_PREFIX + "shopping") is None
    again = await Module().remove(ctx, items=["milk"])
    assert again["status"] == "none_matched"


async def test_remove_needs_items_or_clear() -> None:
    ctx = fake_context(MODULE_DIR, profile="alice")
    with pytest.raises(ToolError):
        await Module().remove(ctx)


async def test_remove_from_empty_list() -> None:
    ctx = fake_context(
        MODULE_DIR, profile="alice", kv={"household": {"list:shopping": {"name": "shopping"}}}
    )
    out = await Module().remove(ctx, items=["milk"])
    assert out["speak"] == "The shopping list is already empty."


# ---------------------------------------------------------------- pure helpers


@pytest.mark.parametrize(
    ("spoken", "name"),
    [
        ("the shopping list", "shopping"),
        ("Groceries", "shopping"),
        ("my to do list", "to-do"),
        ("TODO", "to-do"),
        ("Hardware Store", "hardware store"),
        ("list", "shopping"),
    ],
)
def test_canonical_name(spoken: str, name: str) -> None:
    assert canonical_name(spoken) == name


def test_item_key_folds_case_articles_and_plurals() -> None:
    assert item_key("The Eggs") == item_key("egg")
    assert item_key("glass") == "glass"
    assert item_key("bus") == "bus"


def test_read_text_long_list() -> None:
    texts = [f"item {i}" for i in range(12)]
    out = read_text(texts, "shopping list")
    assert out.startswith("The shopping list has 12 items: item 0, item 1")
    assert out.endswith(", and 2 more.")
    assert read_text([], "your packing list") == "Your packing list is empty."


@given(st.lists(st.text(alphabet="abcdefg ", min_size=1, max_size=12), max_size=15))
def test_merge_then_take_round_trips(items: list[str]) -> None:
    lst = _List("x")
    added, _ = merge(lst, items, NOW)
    keys = [item_key(t) for t in lst.texts]
    assert len(keys) == len(set(keys))  # never two entries for the same item
    removed, missing = take(lst, added)
    assert sorted(removed) == sorted(added)
    assert missing == []
    assert lst.items == []
