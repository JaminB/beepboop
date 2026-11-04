"""music: Music Assistant through Home Assistant (docs/HOME_ASSISTANT.md §12, MODULES §16).

HA-backed: there is no transport here, only ``ctx.ha``. Music Assistant (MA) players are HA
``media_player`` entities; this module picks one and calls ``music_assistant.play_media``
(tier ``act`` in ``policy/ha_services.yaml``), or reads the player's media attributes.
``ctx.ha`` only ever shows entities granted to the caller, and its ``call_service`` re-checks
the grant, so a module bug cannot reach an ungranted speaker.

Which speaker, in order: the room the user named; the caller's ``default_player`` setting
(``x-per-profile``, an entity UUID); the only MA player there is. Otherwise ask.

Pause, skip and volume are ``media.control`` (the ``ha`` module); this module only starts music
and says what is playing. Track titles come from streaming services, so ``music.now_playing``
is ``untrusted_output``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from beepboop.module import (
    EntityRef,
    EntityState,
    Health,
    ToolContext,
    ToolError,
    tool,
)
from beepboop.module import (
    Module as BaseModule,
)

# Music Assistant's HA integration adds this attribute to the media_player entities it owns;
# it is how MA players are told apart from other granted media players (TVs, Cast groups).
MA_ATTRIBUTE = "mass_player_type"
ACTIVE_STATES = frozenset({"playing", "paused", "buffering"})
OFFLINE_STATES = frozenset({"unavailable", "unknown", "off"})
NO_PLAYERS = (
    "No Music Assistant speakers are available to this person. Tell the user an admin can "
    "grant speakers in the beepboop app."
)


@dataclass(frozen=True, slots=True)
class NowPlaying:
    player: str
    state: str
    title: str | None
    artist: str | None
    album: str | None

    def to_output(self) -> dict[str, Any]:
        return {
            "player": self.player,
            "state": self.state,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
        }


class Module(BaseModule):
    async def health(self, ctx: ToolContext) -> Health:
        players = await ma_players(ctx)
        if not players:
            return Health(
                "degraded",
                "No Music Assistant media players are granted yet.",
                spoken="No speakers are set up for music yet. An admin can add them in the "
                "beepboop app.",
            )
        return Health("ok", f"{len(players)} Music Assistant player(s) available.")

    # ------------------------------------------------------------------- tools --

    @tool("music.play")
    async def play(
        self,
        ctx: ToolContext,
        query: str,
        media_type: str | None = None,
        player: str | None = None,
        enqueue: str | None = None,
        radio_mode: bool = False,
    ) -> dict[str, Any]:
        query = " ".join(query.split())
        if not query:
            raise ToolError("Ask the user what to play.", code="invalid_args")
        target = await pick_player(ctx, player)
        if target.state in OFFLINE_STATES:
            raise ToolError(
                f"The {target.entity.name} speaker is offline. Suggest another room.",
                code="unavailable",
                retryable=True,
            )
        data: dict[str, Any] = {"media_id": query}
        if media_type:
            data["media_type"] = media_type
        if enqueue:
            data["enqueue"] = enqueue
        if radio_mode:
            data["radio_mode"] = True
        result = await ctx.ha.call_service("music_assistant", "play_media", [target.entity], data)
        if not result.ok:
            raise ToolError(
                "Music Assistant couldn't play that. Tell the user and suggest another name.",
                code="unavailable",
                retryable=True,
            )
        ctx.log.info("music.play", player=target.entity.uuid, media_type=media_type)
        return {
            "speak": speak_play(query, target.entity.name, enqueue),
            "player": target.entity.name,
            "media_id": query,
            "media_type": media_type,
        }

    @tool("music.now_playing")
    async def now_playing(self, ctx: ToolContext, player: str | None = None) -> dict[str, Any]:
        if player:
            states = [await pick_player(ctx, player)]
        else:
            states = [s for s in await ma_players(ctx) if s.state in ACTIVE_STATES]
        found = [describe(s) for s in states]
        return {
            "speak": speak_now_playing(found, named=player is not None),
            "players": [n.to_output() for n in found],
        }


# ------------------------------------------------------------------ player selection --


def is_ma(state: EntityState) -> bool:
    return MA_ATTRIBUTE in state.attributes


async def ma_players(ctx: ToolContext) -> list[EntityState]:
    """Granted MA players, in HA order. If none carries the MA attribute (an older HA build),
    every granted media player is a candidate and HA rejects the ones MA does not own."""
    states = await ctx.ha.states(["media_player"])
    ma = [s for s in states if is_ma(s)]
    return ma or states


async def pick_player(ctx: ToolContext, player: str | None) -> EntityState:
    """The speaker to use (module docstring order). Raises ``ToolError`` to make the model ask."""
    if player and player.strip():
        return await _named(ctx, player.strip())
    candidates = await ma_players(ctx)
    default = str(ctx.settings.get("default_player") or "").strip()
    if default:
        state = await ctx.ha.state(default)
        if state is not None:
            return state
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ToolError(NO_PLAYERS, code="unavailable")
    names = ", ".join(s.entity.name for s in candidates[:4])
    raise ToolError(f"Ask the user which speaker: {names}.", code="ambiguous")


async def _named(ctx: ToolContext, name: str) -> EntityState:
    refs = await ctx.ha.resolve(name, domains=["media_player"])
    states = [s for s in [await ctx.ha.state(r) for r in refs] if s is not None]
    if not states:
        raise ToolError(
            f"There is no speaker called {name} available. Ask the user for another room.",
            code="not_found",
        )
    ma = [s for s in states if is_ma(s)] or states
    if len(ma) == 1:
        return ma[0]
    exact = [s for s in ma if _same(s.entity, name)]
    if len(exact) == 1:
        return exact[0]
    names = ", ".join(s.entity.name for s in ma[:4])
    raise ToolError(f"Ask the user which speaker: {names}.", code="ambiguous")


def _same(ref: EntityRef, name: str) -> bool:
    wanted = name.casefold()
    return ref.name.casefold() == wanted or any(a.casefold() == wanted for a in ref.aliases)


# --------------------------------------------------------------------- pure helpers --


def _attr(state: EntityState, key: str) -> str | None:
    value = state.attributes.get(key)
    if value is None:
        return None
    text = " ".join(str(value).split())[:200]
    return text or None


def describe(state: EntityState) -> NowPlaying:
    return NowPlaying(
        player=state.entity.name,
        state=state.state,
        title=_attr(state, "media_title"),
        artist=_attr(state, "media_artist") or _attr(state, "media_album_artist"),
        album=_attr(state, "media_album_name"),
    )


def speak_play(query: str, player: str, enqueue: str | None) -> str:
    if enqueue == "next":
        return f"Queued {query} next on {player}."
    if enqueue == "add":
        return f"Added {query} to the queue on {player}."
    return f"Playing {query} on {player}."


def _track(n: NowPlaying) -> str:
    if n.title and n.artist:
        return f"{n.title} by {n.artist}"
    return n.title or n.artist or "something"


def speak_now_playing(found: list[NowPlaying], *, named: bool) -> str:
    """One sentence; at most three speakers, then "and N more"."""
    active = [n for n in found if n.state in ACTIVE_STATES]
    if not active:
        if named and found:
            return f"Nothing is playing on {found[0].player}."
        return "Nothing is playing."
    parts = [
        f"{n.player} {'has' if n.state == 'paused' else 'is playing'} {_track(n)}"
        + (" paused" if n.state == "paused" else "")
        for n in active[:3]
    ]
    more = len(active) - len(parts)
    text = "; ".join(parts) + (f"; and {more} more" if more else "")
    return f"{text}."
