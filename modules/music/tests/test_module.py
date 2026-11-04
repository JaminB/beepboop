"""Tests for the music module (docs/MODULES.md §12.4).

The handlers run against ``fake_context``: ``ctx.ha`` is the dry-run HA client over the
``fixtures.ha.states`` table in ``../evals.yaml`` and records ``call_service`` as an effect.
"""

# Pure helpers are tested directly (MODULES §12.4), so private names are imported on purpose.
# pyright: reportPrivateUsage=false
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from beepboop.module import EntityRef, EntityState, ServiceResult, ToolError
from beepboop.testing import fake_context, run_evals
from beepboop.testing.context import FakeToolContext
from beepboop.testing.fakes import FakeHA

from ..module import Module, NowPlaying, describe, speak_now_playing, speak_play

MODULE_DIR = Path(__file__).resolve().parents[1]
KITCHEN = "01JMUSICKITCHEN0000000000"
LIVING = "01JMUSICLIVING00000000000"


def _ctx(**kw: Any) -> FakeToolContext:
    return fake_context(MODULE_DIR, profile="alice", **kw)


def _row(
    entity_id: str, uuid: str, state: str = "idle", *, ma: bool = True, **attrs: Any
) -> dict[str, Any]:
    attributes: dict[str, Any] = {"friendly_name": attrs.pop("name", entity_id.split(".")[1])}
    if ma:
        attributes["mass_player_type"] = "player"
    attributes.update(attrs)
    return {"entity_id": entity_id, "uuid": uuid, "state": state, "attributes": attributes}


def _ha_calls(ctx: FakeToolContext) -> list[dict[str, Any]]:
    return [e for e in ctx.effects if e["kind"] == "ha"]


# ------------------------------------------------------------- the evals, offline --


def test_evals_pass_offline() -> None:
    report = run_evals(MODULE_DIR)
    assert report.ok, [(r.case_id, r.messages) for r in report.failed]


# ---------------------------------------------------------------------- music.play --


async def test_play_uses_the_default_speaker() -> None:
    ctx = _ctx()
    out = await Module().play(ctx, query="  Queen ", media_type="artist")
    assert out == {
        "speak": "Playing Queen on Kitchen.",
        "player": "Kitchen",
        "media_id": "Queen",
        "media_type": "artist",
    }
    assert _ha_calls(ctx) == [
        {
            "kind": "ha",
            "domain": "music_assistant",
            "service": "play_media",
            "targets": [KITCHEN],
            "data": {"media_id": "Queen", "media_type": "artist"},
        }
    ]


async def test_per_profile_default_wins() -> None:
    ctx = _ctx(settings={"default_player": LIVING})  # Alice's own override (x-per-profile)
    out = await Module().play(ctx, query="jazz")
    assert out["player"] == "Living Room Speaker"
    assert _ha_calls(ctx)[0]["targets"] == [LIVING]


async def test_named_room_prefers_the_music_assistant_player() -> None:
    ctx = _ctx()
    await Module().play(ctx, query="Abbey Road", player="kitchen")
    assert _ha_calls(ctx)[0]["targets"] == [KITCHEN]  # not the Kitchen TV


async def test_enqueue_and_radio_mode_are_passed_through() -> None:
    ctx = _ctx()
    out = await Module().play(ctx, query="Bon Iver", enqueue="add", radio_mode=True)
    assert _ha_calls(ctx)[0]["data"] == {
        "media_id": "Bon Iver",
        "enqueue": "add",
        "radio_mode": True,
    }
    assert out["speak"] == "Added Bon Iver to the queue on Kitchen."


async def test_no_default_and_several_speakers_asks_which() -> None:
    with pytest.raises(ToolError) as e:
        await Module().play(_ctx(settings={"default_player": ""}), query="Queen")
    assert e.value.code == "ambiguous"
    assert "Kitchen" in e.value.message
    assert "Living Room Speaker" in e.value.message
    assert "Kitchen TV" not in e.value.message


async def test_ungranted_default_falls_back_to_the_only_speaker() -> None:
    ha = [_row("media_player.den", "01DEN", name="Den")]
    ctx = _ctx(settings={"default_player": "01NOTGRANTED"}, ha=ha)
    out = await Module().play(ctx, query="Queen")
    assert out["player"] == "Den"


async def test_no_speakers_is_unavailable_and_health_degraded() -> None:
    ctx = _ctx(ha=[])
    with pytest.raises(ToolError) as e:
        await Module().play(ctx, query="Queen", player=None)
    assert e.value.code in ("unavailable", "not_found")
    assert (await Module().health(fake_context(MODULE_DIR, ha=[]))).status == "degraded"


async def test_health_ok_counts_players() -> None:
    health = await Module().health(fake_context(MODULE_DIR))
    assert health.status == "ok"
    assert health.detail.startswith("2 ")


async def test_unknown_room_is_not_found() -> None:
    with pytest.raises(ToolError) as e:
        await Module().play(_ctx(), query="Queen", player="garage")
    assert e.value.code == "not_found"


async def test_offline_speaker_is_unavailable() -> None:
    ha = [_row("media_player.den", "01DEN", "unavailable", name="Den")]
    with pytest.raises(ToolError) as e:
        await Module().play(_ctx(ha=ha, settings={"default_player": ""}), query="Queen")
    assert e.value.code == "unavailable"
    assert "Den" in e.value.message


async def test_two_matches_ask_unless_one_is_exact() -> None:
    ha = [
        _row("media_player.bedroom", "01B1", name="Bedroom"),
        _row("media_player.bedroom_2", "01B2", name="Bedroom Speaker"),
    ]
    out = await Module().play(_ctx(ha=ha), query="Queen", player="bedroom")
    assert out["player"] == "Bedroom"
    with pytest.raises(ToolError) as e:
        await Module().play(_ctx(ha=ha), query="Queen", player="bed")
    assert e.value.code == "ambiguous"


async def test_without_ma_attribute_every_media_player_is_a_candidate() -> None:
    ha = [_row("media_player.den", "01DEN", ma=False, name="Den")]
    out = await Module().play(_ctx(ha=ha, settings={"default_player": ""}), query="Queen")
    assert out["player"] == "Den"


async def test_blank_query_is_invalid() -> None:
    with pytest.raises(ToolError) as e:
        await Module().play(_ctx(), query="   ")
    assert e.value.code == "invalid_args"


class _FailingHA(FakeHA):
    async def call_service(
        self,
        domain: str,
        service: str,
        targets: list[EntityRef],
        data: dict[str, Any] | None = None,
    ) -> ServiceResult:
        return ServiceResult(ok=False, detail="no match")


async def test_failed_service_call_is_unavailable() -> None:
    ctx = _ctx()
    assert isinstance(ctx.ha, FakeHA)
    failing = _FailingHA(ctx.ha.states_table, ctx.effects, in_tool_call=True)
    with pytest.raises(ToolError) as e:
        await Module().play(replace(ctx, ha=failing), query="Nonexistent band")
    assert e.value.code == "unavailable"


async def test_play_never_logs_the_query() -> None:
    ctx = _ctx()
    await Module().play(ctx, query="Secret Crush Playlist")
    assert "secret crush" not in repr(ctx.log_calls).lower()


# --------------------------------------------------------------- music.now_playing --


async def test_now_playing_reports_active_speakers() -> None:
    ctx = _ctx()
    out = await Module().now_playing(ctx)
    assert out["players"] == [
        {
            "player": "Living Room Speaker",
            "state": "playing",
            "title": "Bohemian Rhapsody",
            "artist": "Queen",
            "album": "A Night at the Opera",
        }
    ]
    assert out["speak"] == "Living Room Speaker is playing Bohemian Rhapsody by Queen."
    assert ctx.effects == []


async def test_now_playing_named_idle_room() -> None:
    out = await Module().now_playing(_ctx(), player="kitchen")
    assert out["speak"] == "Nothing is playing on Kitchen."
    assert out["players"][0]["title"] is None


async def test_now_playing_nothing_anywhere() -> None:
    ha = [_row("media_player.den", "01DEN", name="Den")]
    out = await Module().now_playing(_ctx(ha=ha))
    assert out == {"speak": "Nothing is playing.", "players": []}


async def test_now_playing_paused_and_many() -> None:
    ha = [
        _row(
            f"media_player.p{i}",
            f"01P{i}",
            "paused" if i == 0 else "playing",
            name=f"P{i}",
            media_title=f"T{i}",
            media_artist=f"A{i}",
        )
        for i in range(5)
    ]
    out = await Module().now_playing(_ctx(ha=ha))
    assert out["speak"] == (
        "P0 has T0 by A0 paused; P1 is playing T1 by A1; P2 is playing T2 by A2; and 2 more."
    )
    assert len(out["players"]) == 5


# --------------------------------------------------------------------- pure helpers --


def _state(**attrs: Any) -> EntityState:
    ref = EntityRef(uuid="01X", entity_id="media_player.x", domain="media_player", name="X")
    return EntityState(ref, "playing", attrs)


def test_describe_normalises_and_falls_back() -> None:
    n = describe(_state(media_title="  Song \n Title ", media_album_artist="Band"))
    assert (n.title, n.artist, n.album) == ("Song Title", "Band", None)
    assert describe(_state(media_title="x" * 500)).title == "x" * 200


@pytest.mark.parametrize(
    ("enqueue", "spoken"),
    [
        (None, "Playing Q on K."),
        ("play", "Playing Q on K."),
        ("replace", "Playing Q on K."),
        ("next", "Queued Q next on K."),
        ("add", "Added Q to the queue on K."),
    ],
)
def test_speak_play(enqueue: str | None, spoken: str) -> None:
    assert speak_play("Q", "K", enqueue) == spoken


_words = st.text(alphabet="abcdefghij XYZ", max_size=30)
_np = st.builds(
    NowPlaying,
    player=st.text(alphabet="abcdefghij XYZ", min_size=1, max_size=20),
    state=st.sampled_from(["playing", "paused", "idle", "off", "buffering"]),
    title=st.none() | _words,
    artist=st.none() | _words,
    album=st.none() | _words,
)


@given(st.lists(_np, max_size=8), st.booleans())
def test_speak_now_playing_is_one_sentence_naming_at_most_three(
    found: list[NowPlaying], named: bool
) -> None:
    text = speak_now_playing(found, named=named)
    assert text.endswith(".")
    active = [n for n in found if n.state in ("playing", "paused", "buffering")]
    assert text.count("; ") <= 3
    if len(active) > 3:
        assert text.endswith(f"and {len(active) - 3} more.")
