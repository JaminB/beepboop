"""Provider registration by entry point (VOICE_PIPELINE §17, CLAUDE.md §7.1).

Groups: ``beepboop.wake_providers``, ``beepboop.speaker_providers``,
``beepboop.stt_providers``, ``beepboop.tts_providers``. Each entry point names
the provider class; ids are the hyphenated ones (``sherpa-campp``). The
provider is chosen by ``providers:`` in ``/config/beepboop.yaml``, never by an
environment variable or an event.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Literal

Kind = Literal["wake", "speaker", "stt", "tts"]
KINDS: tuple[Kind, ...] = ("wake", "speaker", "stt", "tts")


class UnknownProviderError(LookupError):
    pass


def group(kind: Kind) -> str:
    return f"beepboop.{kind}_providers"


def available(kind: Kind) -> list[str]:
    return sorted(ep.name for ep in entry_points(group=group(kind)))


def provider_class(kind: Kind, provider_id: str) -> type:
    for ep in entry_points(group=group(kind)):
        if ep.name == provider_id:
            loaded: object = ep.load()
            if not isinstance(loaded, type):
                raise UnknownProviderError(f"{group(kind)}:{provider_id} is not a class")
            return loaded
    raise UnknownProviderError(
        f"no {kind} provider {provider_id!r}; installed: {', '.join(available(kind)) or 'none'}"
    )


__all__ = ["KINDS", "Kind", "UnknownProviderError", "available", "group", "provider_class"]
