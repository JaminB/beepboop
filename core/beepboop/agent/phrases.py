"""System phrases and the deny → phrase rules (docs/VOICE_PIPELINE.md §11.6 is normative).

Core sends a phrase by key (``speak{clip}``). Keys whose text has a ``{placeholder}`` are
sent as ``clip`` **and** the filled ``text``: the audio service has no pre-rendered clip
for them and synthesises the text (ARCHITECTURE §4.2 clip-key rule). The English texts
below are the §11.6 defaults; the persona module may restyle them, the meaning may not
change.

The two §11.6 rules are enforced here and tested:

1. never tell a Guest (or unrecognised speaker) to approve on a phone;
2. never say a tier name (``read``/``act``/``personal``/``critical``) aloud.
"""

from __future__ import annotations

import re
from typing import Final

from beepboop.agent.types import Speech
from beepboop.policy.types import DenyReason, ProfileKind

#: VOICE_PIPELINE §11.6, en-US defaults. Order follows the table.
PHRASES: Final[dict[str, str]] = {
    "claim.code": "Your setup code is {d1} {d2} {d3} {d4} {d5} {d6}.",
    "claim.fingerprint": "My certificate fingerprint starts {f1} {f2} {f3} {f4} {f5} {f6}.",
    "approval.prompt": "To {summary}, approve on your phone.",
    "approval.prompt_pin": "To {summary}, approve on your phone, or say your PIN.",
    "approval.waiting": "Still waiting for your phone.",
    "approval.done": "Done. {summary_past}.",
    "approval.denied": "Okay, I won't.",
    "approval.expired": (
        "That approval timed out, so I didn't do it. Ask again when your phone's ready."
    ),
    "approval.no_login": (
        "You haven't set up beepboop on your phone yet. Ask {admin} to send you an invite."
    ),
    "personal.unsure": (
        "That sounded like you, {name}, but I'm not sure enough for your {thing}. "
        "Approve on your phone, or say your PIN."
    ),
    "unknown_speaker": (
        "I didn't recognise your voice, so I can't do that. "
        "If you live here, try again a bit closer."
    ),
    "ceiling.child": "I can't do that for you, {name}. Ask a grown-up.",
    "not_granted": "You don't have access to the {thing}. An adult can change that in the app.",
    "pin.prompt": "Please say your PIN.",
    "pin.mismatch": "That wasn't it. Approve on your phone instead.",
    "pin.locked": "Your PIN is locked for a few minutes. Use your phone.",
    "offline": "I can't reach my brain right now, but I can still control the house.",
    "reauth": "Your {integration} needs reconnecting in the app.",
    "not_me": "Okay, noted that wasn't you.",
    "forget": "Okay, I've forgotten that.",
    "cancel": "Cancelled.",
}

#: Spoken without a §11.6 key (ARCHITECTURE §10, LLM_PROVIDERS §3.6): plain ``speak{text}``.
SETUP_NEEDED: Final = "I can control the house; finish setup to let me chat."
LOST_CONNECTION: Final = "Sorry, I lost my connection."
TOO_MANY_PENDING: Final = "You already have approvals waiting on your phone."
TOOL_FAILED: Final = "Sorry, that didn't work."

#: deny reason → phrase key (VOICE_PIPELINE §11.6); :func:`deny_phrase` applies the rules.
DENY_PHRASES: Final[dict[DenyReason, str]] = {
    "ceiling": "ceiling.child",
    "no_grant": "not_granted",
    "schedule": "not_granted",
    "scope": "not_granted",
    "service_blocked": "not_granted",
    "disabled": "unknown_speaker",
    "rate_limited": "not_granted",
    "identity": "unknown_speaker",
    "impersonation": "not_granted",
    "no_factor": "approval.no_login",
    "approval_invalid": "approval.expired",
}

#: Tier vocabulary that must never be spoken (§11.6 rule 2).
TIER_WORDS: Final = re.compile(r"\b(act|personal|critical)\b", re.IGNORECASE)

_PLACEHOLDER = re.compile(r"\{(\w+)\}")

#: The ``{thing}`` a denial names, by tool namespace. Phrases name the thing, never the tier.
_THINGS: Final[dict[str, str]] = {
    "home": "device",
    "media": "player",
    "timer": "timers",
    "list": "lists",
    "weather": "weather",
    "caldav": "calendar",
    "music": "music",
    "persona": "voice settings",
}


def deny_phrase(reason: DenyReason, kind: ProfileKind, via: str = "voice") -> str:
    """Never tell a guest to approve on a phone; only children hear ``ceiling.child``. A
    guest denial speaks ``unknown_speaker`` only when a voice was involved; a typed turn
    (``beepboop chat --as guest``, the web) hears ``not_granted`` instead."""
    if kind == "guest":
        return "unknown_speaker" if via == "voice" else "not_granted"
    key = DENY_PHRASES[reason]
    if key == "ceiling.child" and kind != "child":
        return "not_granted"
    return key


def thing_for(tool: str, spoken_name: str | None = None) -> str:
    """What a denial calls the thing the person asked about ("the calendar", never
    "personal"): the module's manifest ``spoken_name`` when it has one, else the
    built-in table for the shipped namespaces, else the namespace in plain words."""
    if spoken_name:
        return spoken_name
    namespace = tool.split(".", 1)[0]
    return _THINGS.get(namespace, namespace.replace("_", " "))


def placeholders(key: str) -> frozenset[str]:
    return frozenset(_PLACEHOLDER.findall(PHRASES[key]))


def render(key: str, **slots: str) -> str:
    """The §11.6 text for ``key`` with ``slots`` filled. Missing slots raise ``KeyError``."""
    return PHRASES[key].format(**slots)


def phrase(key: str, *, listen_after: bool = False, **slots: str) -> Speech:
    """One ``Speech`` for a §11.6 key: ``clip`` alone, or ``clip`` + filled ``text``."""
    if key not in PHRASES:
        raise KeyError(key)
    if not placeholders(key):
        return Speech(clip=key, listen_after=listen_after)
    return Speech(text=render(key, **slots), clip=key, listen_after=listen_after)


def deny_speech(
    reason: DenyReason,
    kind: ProfileKind,
    *,
    tool: str,
    name: str,
    admin: str,
    via: str = "voice",
    spoken_name: str | None = None,
) -> Speech:
    """The spoken denial for one ``PolicyDenied``; rules 1 and 2 hold by construction."""
    key = deny_phrase(reason, kind, via)
    return phrase(key, name=name, thing=thing_for(tool, spoken_name), admin=admin, summary="that")


__all__ = [
    "DENY_PHRASES",
    "LOST_CONNECTION",
    "PHRASES",
    "SETUP_NEEDED",
    "TIER_WORDS",
    "TOOL_FAILED",
    "TOO_MANY_PENDING",
    "deny_phrase",
    "deny_speech",
    "phrase",
    "placeholders",
    "render",
    "thing_for",
]
