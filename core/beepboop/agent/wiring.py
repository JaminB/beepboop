"""Build the production ``TurnLoop`` from the running services (core/README.md "How core starts").

``build_turn_loop`` is what ``CoreService.start`` calls: it reads the household ``llm``
document (LLM_PROVIDERS §2.1; ``providers/llm/defaults.yaml`` until the System LLM page is saved)
and the provider secrets, builds the fallback ladder, and wires the database-backed
conversation window, ``turns`` log, profile directory and prompt assembly. Missing
credentials are not an error at boot: the loop runs fast-path-only and says the setup
sentence (ARCHITECTURE §10).
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, tzinfo
from typing import Any, cast

from pydantic import SecretStr

from beepboop.agent.context import PromptInputs, build_prompt_inputs
from beepboop.agent.fastpath import FastPath
from beepboop.agent.records import ConversationTaint, DbConversation, DbDirectory, DbTurnLog
from beepboop.agent.turn import NoLLM, TurnLoop
from beepboop.agent.types import ConversationStore, PinStore, Speaker, Utterance, VoiceControl
from beepboop.db import Database, repo
from beepboop.log import get_logger
from beepboop.module.context import ToolContext
from beepboop.providers.llm.fallback import (
    BEDROCK_SECRETS,
    Ladder,
    LadderSetupError,
    build_ladder,
    default_llm_config,
)
from beepboop.providers.llm.protocol import AuthError, ProviderError
from beepboop.registry.registry import Registry
from beepboop.registry.resolver import EntityIndex
from beepboop.secrets import SecretRef, SecretStore

log = get_logger(__name__)


async def load_llm_secrets(secrets: SecretStore | None) -> dict[str, SecretStr]:
    """The provider secrets present in the store (``owner_module="core"``)."""
    out: dict[str, SecretStr] = {}
    if secrets is None:
        return out
    for name in BEDROCK_SECRETS:
        ref = SecretRef(name, owner_module="core")
        with contextlib.suppress(Exception):
            if await secrets.exists(ref):
                out[name] = await secrets.get(ref)
    return out


async def load_provider(db: Database, secrets: SecretStore | None) -> Ladder | NoLLM:
    """The ladder from ``household.llm`` (or the defaults), or ``NoLLM("unconfigured")``."""
    stored = await db.read(lambda c: repo.household.get(c, "llm"))
    config = cast(Mapping[str, Any], stored) if isinstance(stored, dict) else default_llm_config()
    try:
        ladder = build_ladder(config, await load_llm_secrets(secrets))
    except (AuthError, LadderSetupError) as exc:
        log.info("llm.unconfigured", reason=exc.code)
        return NoLLM("unconfigured")
    except (ProviderError, KeyError, ValueError) as exc:
        log.warning("llm.config_invalid", error=type(exc).__name__)
        return NoLLM("unconfigured")
    return ladder if ladder.rungs else NoLLM("unconfigured")


async def build_turn_loop(
    *,
    db: Database,
    registry: Registry,
    secrets: SecretStore | None,
    speaker: Speaker,
    conversation: ConversationStore | None = None,
    locale: str = "en-US",
    tz: tzinfo = UTC,
    hook_context: Callable[[str, str | None], ToolContext] | None = None,
    entity_index: Callable[[], EntityIndex | None] = lambda: None,
    control: VoiceControl | None = None,
    pins: PinStore | None = None,
    provider: Ladder | NoLLM | None = None,
) -> TurnLoop:
    """The production loop. Also points ``registry.taint`` at the conversation row."""
    store = conversation or DbConversation(db)
    registry.taint = ConversationTaint(store)
    directory = DbDirectory(db)
    await directory.refresh(force=True)

    async def persona_block(profile_id: str) -> str:
        if hook_context is None:
            return ""
        blocks: list[str] = []
        for manifest, module in registry.modules():
            if manifest.prompt_block is None:
                continue
            try:
                block = await module.prompt_block(hook_context(manifest.id, profile_id))
            except Exception as exc:
                log.warning(
                    "persona.prompt_block_failed", module_id=manifest.id, error=type(exc).__name__
                )
                continue
            if block is not None and block.text:
                blocks.append(block.text)
        return "\n\n".join(blocks)

    async def prompt_inputs(utterance: Utterance) -> PromptInputs:
        p = utterance.principal
        callable_names = {t.name for t in registry.callable_by(p)}
        return build_prompt_inputs(
            registry,
            profile_id=p.profile_id,
            speaker_name=directory.name(p.profile_id),
            speaker_kind=directory.kind(p.profile_id),
            callable_names=callable_names,
            persona_block=await persona_block(p.profile_id),
            local_time=datetime.now(tz).isoformat(timespec="minutes"),
            device_area=utterance.device,
        )

    return TurnLoop(
        registry=registry,
        speaker=speaker,
        conversation=store,
        prompt_inputs=prompt_inputs,
        provider=provider if provider is not None else await load_provider(db, secrets),
        fastpath=FastPath.for_locale(locale),
        directory=directory,
        tz=tz,
        entity_index=entity_index,
        turn_log=DbTurnLog(db),
        control=control,
        pins=pins,
    )


__all__ = ["build_turn_loop", "load_llm_secrets", "load_provider"]
