"""persona: the household personality and each person's voice, rate and verbosity.

Two layers (docs/MODULES.md §5.6, §6.3, §16):

* ``ctx.settings``: the household default, merged by core with the person's own
  ``x-per-profile`` choices on the persona Integrations page (voice, speech_rate, verbosity).
* ``ctx.kv`` profile scope, key ``overrides``: what the person changed by voice with
  ``persona.set`` ("talk slower", "be briefer"). These win over settings for that person
  and are deleted with the profile.

``prompt_block`` renders a short, deterministic system-prompt block for one profile from
those two layers; it never includes time or per-turn state. ``effective()`` is the one
place the layers are combined, so the speech side (voice, rate) reads the same values.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from beepboop.module import KVStore, PromptBlock, ToolContext, ToolError, tool
from beepboop.module import Module as BaseModule

Verbosity = Literal["brief", "normal", "chatty"]
KV_OVERRIDES = "overrides"
RATE_MIN, RATE_MAX, RATE_STEP = 0.7, 1.4, 0.1
_VERBOSITY: tuple[Verbosity, ...] = ("brief", "normal", "chatty")

STYLE_LINES: Mapping[str, str] = {
    "warm": "Be warm and friendly, like a helpful member of the household.",
    "calm": "Be calm and gentle; keep a steady, unhurried tone.",
    "playful": "Be light and a little playful, but never at the expense of a clear answer.",
    "matter_of_fact": "Be matter-of-fact: no small talk, no exclamations.",
}
VERBOSITY_LINES: Mapping[Verbosity, str] = {
    "brief": "This person prefers brief answers: one short sentence, no pleasantries.",
    "normal": "Answer in one or two short sentences.",
    "chatty": "This person likes a little more detail: up to three sentences when useful.",
}


@dataclass(frozen=True, slots=True)
class Persona:
    name: str
    style: str
    verbosity: Verbosity
    voice: str
    speech_rate: float


def _verbosity(value: object, default: Verbosity = "normal") -> Verbosity:
    return value if value in _VERBOSITY else default


def _rate(value: object, default: float = 1.0) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return clamp_rate(float(value))


def clamp_rate(rate: float) -> float:
    return round(min(RATE_MAX, max(RATE_MIN, rate)), 1)


def effective(settings: Mapping[str, Any], overrides: Mapping[str, Any]) -> Persona:
    """Household settings (already merged with per-profile settings) ← voice overrides."""
    name = str(settings.get("name") or "beepboop").strip()[:32] or "beepboop"
    style = str(settings.get("style") or "warm")
    base_verbosity = _verbosity(settings.get("verbosity"))
    base_rate = _rate(settings.get("speech_rate"))
    return Persona(
        name=name,
        style=style if style in STYLE_LINES else "warm",
        verbosity=_verbosity(overrides.get("verbosity"), base_verbosity),
        voice=str(settings.get("voice") or "en_US-lessac-low"),
        speech_rate=_rate(overrides.get("speech_rate"), base_rate),
    )


def render_block(p: Persona) -> str:
    """The system-prompt block: stable for a given persona, well under the 300-token budget."""
    lines = [
        f'The household calls you "{p.name}"; answer to that name.'
        if p.name.lower() != "beepboop"
        else "",
        STYLE_LINES[p.style],
        VERBOSITY_LINES[p.verbosity],
    ]
    if p.speech_rate < 1.0:
        lines.append("This person asked for slower speech: prefer short words and short sentences.")
    return "\n".join(line for line in lines if line)


async def _overrides(kv: KVStore) -> dict[str, Any]:
    raw: object = await kv.get(KV_OVERRIDES, {})
    return dict(cast(dict[str, Any], raw)) if isinstance(raw, dict) else {}


class Module(BaseModule):
    """Persona prompt block plus the one voice tool, ``persona.set``."""

    async def prompt_block(self, ctx: ToolContext) -> PromptBlock | None:
        overrides = await _overrides(ctx.kv) if ctx.profile_id else {}
        return PromptBlock(render_block(effective(ctx.settings, overrides)))

    @tool("persona.set")
    async def set_persona(
        self,
        ctx: ToolContext,
        speech_rate: Literal["slower", "faster", "normal"] | None = None,
        verbosity: Verbosity | None = None,
        reset: bool = False,
    ) -> dict[str, Any]:
        if not ctx.profile_id:
            raise ToolError("Persona changes need to know who is asking.", code="other")
        if not reset and speech_rate is None and verbosity is None:
            raise ToolError(
                "Ask whether the user wants slower or faster speech, or shorter or longer answers.",
                code="invalid_args",
            )
        before = effective(ctx.settings, await _overrides(ctx.kv))
        if reset:
            await ctx.kv.delete(KV_OVERRIDES)
            after = effective(ctx.settings, {})
            ctx.log.info("persona.reset")
            return _out("Okay, back to the household defaults.", after)
        overrides = await _overrides(ctx.kv)
        household = effective(ctx.settings, {})
        notes: list[str] = []
        if speech_rate == "normal":
            overrides.pop("speech_rate", None)
            notes.append("I'll speak at the usual speed")
        elif speech_rate is not None:
            step = -RATE_STEP if speech_rate == "slower" else RATE_STEP
            new = clamp_rate(before.speech_rate + step)
            if new == before.speech_rate:
                notes.append(
                    "that's as slow as I go"
                    if speech_rate == "slower"
                    else "that's as fast as I go"
                )
            else:
                notes.append(f"I'll speak {speech_rate}")
            if new == household.speech_rate:
                overrides.pop("speech_rate", None)
            else:
                overrides["speech_rate"] = new
        if verbosity is not None:
            if verbosity == household.verbosity:
                overrides.pop("verbosity", None)
            else:
                overrides["verbosity"] = verbosity
            notes.append(
                {
                    "brief": "I'll keep it brief",
                    "normal": "I'll answer normally",
                    "chatty": "I'll give you a bit more detail",
                }[verbosity]
            )
        if overrides:
            await ctx.kv.set(KV_OVERRIDES, overrides)
        else:
            await ctx.kv.delete(KV_OVERRIDES)
        after = effective(ctx.settings, overrides)
        ctx.log.info("persona.set", rate=after.speech_rate, verbosity=after.verbosity)
        text = "Okay, " + " and ".join(notes) + "."
        return _out(text, after)


def _out(speak: str, p: Persona) -> dict[str, Any]:
    return {
        "speak": speak,
        "speech_rate": p.speech_rate,
        "verbosity": p.verbosity,
        "voice": p.voice,
    }
