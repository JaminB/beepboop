"""Unit tests for the `persona` module (docs/MODULES.md §12.4)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from beepboop import tokens
from beepboop.module import ToolError
from beepboop.testing import fake_context

from ..module import (
    KV_OVERRIDES,
    RATE_MAX,
    RATE_MIN,
    STYLE_LINES,
    Module,
    clamp_rate,
    effective,
    render_block,
)

MODULE_DIR = Path(__file__).resolve().parents[1]


async def test_slower_writes_only_the_speakers_override() -> None:
    ctx = fake_context(MODULE_DIR, profile="alice")
    out = await Module().set_persona(ctx, speech_rate="slower")
    assert out == {
        "speak": "Okay, I'll speak slower.",
        "speech_rate": 0.9,
        "verbosity": "normal",
        "voice": "en_US-lessac-low",
    }
    assert await ctx.kv.get(KV_OVERRIDES) == {"speech_rate": 0.9}
    assert await ctx.kv.household().get(KV_OVERRIDES) is None


async def test_limits_and_returning_to_default_clear_the_override() -> None:
    ctx = fake_context(MODULE_DIR, profile="admin")  # fixture: already at 0.7
    out = await Module().set_persona(ctx, speech_rate="slower")
    assert out["speak"] == "Okay, that's as slow as I go."
    assert out["speech_rate"] == RATE_MIN
    alice = fake_context(MODULE_DIR, profile="alice")
    await Module().set_persona(alice, speech_rate="faster")
    back = await Module().set_persona(alice, speech_rate="slower")
    assert back["speech_rate"] == 1.0
    assert await alice.kv.get(KV_OVERRIDES) is None  # equal to household → no override kept


async def test_verbosity_and_reset() -> None:
    ctx = fake_context(MODULE_DIR, profile="alice")
    out = await Module().set_persona(ctx, speech_rate="slower", verbosity="chatty")
    assert out["speak"] == "Okay, I'll speak slower and I'll give you a bit more detail."
    reset = await Module().set_persona(ctx, reset=True)
    assert reset["speech_rate"] == 1.0
    assert reset["verbosity"] == "normal"
    assert ctx.effects[-1] == {"kind": "kv", "op": "delete", "key": KV_OVERRIDES}


async def test_normal_speed_drops_only_the_rate() -> None:
    ctx = fake_context(
        MODULE_DIR,
        profile="alice",
        kv={"alice": {KV_OVERRIDES: {"speech_rate": 1.2, "verbosity": "brief"}}},
    )
    out = await Module().set_persona(ctx, speech_rate="normal")
    assert out["speech_rate"] == 1.0
    assert await ctx.kv.get(KV_OVERRIDES) == {"verbosity": "brief"}


async def test_needs_something_to_change_and_a_profile() -> None:
    with pytest.raises(ToolError) as e:
        await Module().set_persona(fake_context(MODULE_DIR, profile="alice"))
    assert e.value.code == "invalid_args"
    with pytest.raises(ToolError):
        await Module().set_persona(fake_context(MODULE_DIR), reset=True)


async def test_prompt_block_uses_settings_and_profile_overrides() -> None:
    hook = fake_context(
        MODULE_DIR,
        profile="alice",
        in_tool_call=False,
        settings={"name": "Robo", "style": "calm"},
        kv={"alice": {KV_OVERRIDES: {"verbosity": "brief", "speech_rate": 0.8}}},
    )
    block = await Module().prompt_block(hook)
    assert block is not None
    assert 'calls you "Robo"' in block.text
    assert STYLE_LINES["calm"] in block.text
    assert "brief answers" in block.text
    assert "slower speech" in block.text
    household = await Module().prompt_block(fake_context(MODULE_DIR))
    assert household is not None
    assert "Robo" not in household.text


def test_effective_ignores_garbage() -> None:
    settings: dict[str, Any] = {
        "style": "evil",
        "verbosity": "loud",
        "speech_rate": "fast",
        "name": "  ",
    }
    p = effective(settings, {"speech_rate": True, "verbosity": 3})
    assert (p.name, p.style, p.verbosity, p.speech_rate) == ("beepboop", "warm", "normal", 1.0)


@given(
    st.sampled_from(sorted(STYLE_LINES)),
    st.sampled_from(["brief", "normal", "chatty"]),
    st.floats(min_value=0.0, max_value=3.0),
    st.text(max_size=40),
)
def test_block_is_deterministic_and_within_budget(
    style: str, verbosity: str, rate: float, name: str
) -> None:
    settings = {"style": style, "verbosity": verbosity, "speech_rate": rate, "name": name}
    a = render_block(effective(settings, {}))
    assert a == render_block(effective(dict(settings), {}))
    assert tokens.estimate(a) <= 300


@given(st.floats(min_value=-10, max_value=10, allow_nan=False))
def test_clamp_rate_stays_in_range(rate: float) -> None:
    assert RATE_MIN <= clamp_rate(rate) <= RATE_MAX
