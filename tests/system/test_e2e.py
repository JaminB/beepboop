"""The whole system on x86: core ⇄ audio socket ⇄ fake audio, fake LLM, fake HA.

``CoreService.start`` boots the real core (database, envelope secrets, HA client, registry
with every in-repo module, agent loop, audio bridge, API). The audio side is a scripted
:class:`FakeAudio` listening on loopback TCP exactly as ``beepboop-audio`` does
(``AudioEventServer``); core connects with its real ``CoreEventClient``. HA is the shared
fake WebSocket (``beepboop.testing.fake_ha``) and the LLM is ``ScriptedProvider``.

Scenarios (ARCHITECTURE §4.3, §5; ROADMAP v0.1 acceptance 4-6):

1. wake → speaker ID → "turn off the kitchen lights" on the **fast path**;
2. a tool turn through the **LLM** (``ack`` earcon, streamed sentence);
3. a **critical** action ("unlock the front door") that pauses for **web approval**, is
   approved from the speaker's own web session, executes once and is spoken.

Nothing here is a latency measurement: the waits are generous timeouts, not targets.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from beepboop.agent import NoLLM
from beepboop.api.events import Viewer
from beepboop.health import HealthReport
from beepboop.testing.llm import ScriptedProvider, text, tool_call
from beepboop_protocol import (
    EarconEvent,
    ProfilesSyncEvent,
    SpeakEvent,
)

from .core_rig import PUBLIC, SAME_ORIGIN, SESSION_COOKIE, WAIT_S, Booted, Voiceprint, boot_core
from .fake_audio import MODEL_ID, FakeAudio

ALEX_CENTROID = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


# ------------------------------------------------------------------ the house --


@dataclass
class House(Booted):
    audio: FakeAudio = field(default_factory=FakeAudio)
    http: AsyncClient | None = None
    sync: ProfilesSyncEvent | None = None

    def llm(self, *steps: Any) -> ScriptedProvider:
        provider = ScriptedProvider(list(steps))
        self.service.loop.provider = provider
        return provider

    def web(self, profile_id: str) -> AsyncClient:
        """The API client, logged in as ``profile_id``'s web session."""
        assert self.http is not None
        self.http.cookies.set(SESSION_COOKIE, self.tokens[profile_id])
        return self.http


@pytest.fixture
async def house(tmp_path: Path) -> AsyncIterator[House]:
    audio = FakeAudio()
    address = await audio.start()
    voiceprint = Voiceprint(MODEL_ID, "0" * 16, ALEX_CENTROID)
    try:
        async with boot_core(tmp_path, address, voiceprint) as core:
            await asyncio.wait_for(audio.connected.wait(), WAIT_S)
            sync: ProfilesSyncEvent = await audio.next(ProfilesSyncEvent)
            transport = ASGITransport(app=core.service.app)
            async with AsyncClient(transport=transport, base_url=PUBLIC) as http:
                yield House(
                    core.service, core.ha, core.alex, core.sam, core.tokens, audio, http, sync
                )
    finally:
        await audio.close()


# ------------------------------------------------------------------ scenarios --


async def test_profiles_sync_carries_the_sealed_voiceprint(house: House) -> None:
    assert house.sync is not None
    rows = {p.profile_id: p for p in house.sync.data.profiles}
    assert house.sync.data.model_id == MODEL_ID
    assert rows[house.alex].centroids == [ALEX_CENTROID]  # unsealed only to send it
    assert rows[house.sam].centroids == []  # not enrolled
    assert rows["guest"].centroids == []
    assert {"alex", "sam", "kitchen"} <= set(rows["guest"].hotwords)  # household list
    assert rows[house.alex].voice.voice_id == "en_US-lessac-low"


async def test_fast_path_turns_off_the_kitchen_lights(house: House) -> None:
    provider = house.llm(text("The LLM must not be asked."))
    turn_id = await house.audio.say("turn off the kitchen lights", profile_id=house.alex)
    reply = await house.audio.speak_reply(turn_id)
    assert len(reply) == 1
    assert reply[0].data.text  # the handler's spoken line ("done" clip when it has none)
    assert house.ha_calls() == [
        ("light", "turn_off", ["light.kitchen_ceiling", "light.kitchen_counter"])
    ]
    row = await house.turn_row(turn_id)
    assert (row.path, row.profile_id, row.via, row.id_stage) == (
        "fast",
        house.alex,
        "voice",
        "final",
    )
    calls = await house.tool_calls(turn_id)
    assert [(c.tool, c.decision, c.effective_risk) for c in calls] == [
        ("home.turn_off", "allow", "act")
    ]
    assert provider.requests == []  # the fast path never asks the LLM


async def test_llm_tool_turn_acks_then_speaks(house: House) -> None:
    provider = house.llm(
        tool_call("c1", "home.turn_off", {"target": {"name": "lamp", "area": "living room"}}),
        text("The living room lamp is off."),
    )
    turn_id = await house.audio.say(
        "it is far too bright in here, could you do something about the lamp",
        profile_id=house.alex,
    )
    ack: EarconEvent = await house.audio.next(EarconEvent, turn_id=turn_id)
    assert ack.data.name == "ack"
    reply = await house.audio.speak_reply(turn_id)
    assert reply[-1].data.text == "The living room lamp is off."
    assert ("light", "turn_off", ["light.living_lamp"]) in house.ha_calls()
    assert len(provider.requests) == 2  # tool call, then the answer
    row = await house.turn_row(turn_id)
    assert (row.path, row.profile_id) == ("llm", house.alex)
    assert row.tokens_in > 0
    assert [c.tool for c in await house.tool_calls(turn_id)] == ["home.turn_off"]


async def test_critical_action_waits_for_web_approval(house: House) -> None:
    assert house.service.api is not None
    alex_stream = house.service.api.events.subscribe(Viewer(house.alex, "admin"))[1]
    house.llm(tool_call("c1", "home.turn_off", {"target": {"name": "front door"}}))
    turn_id = await house.audio.say("unlock the front door", profile_id=house.alex)

    prompt = (await house.audio.speak_reply(turn_id))[-1]
    assert prompt.data.clip == "approval.prompt"
    assert prompt.data.text is not None
    assert "approve on your phone" in prompt.data.text
    assert house.ha_calls() == []  # nothing ran yet

    # The banner event reaches Alex's web session with the nonce (SSE payload).
    event = await asyncio.wait_for(alex_stream.get(), WAIT_S)
    assert event.event == "approval"
    approval_id, nonce = str(event.data["id"]), str(event.data["nonce"])

    # Sam's session cannot approve Alex's request.
    r = await house.web(house.sam).post(
        f"/api/approvals/{approval_id}/approve", json={"nonce": nonce}, headers=SAME_ORIGIN
    )
    assert r.status_code == 409
    assert house.ha_calls() == []

    r = await house.web(house.alex).post(
        f"/api/approvals/{approval_id}/approve", json={"nonce": nonce}, headers=SAME_ORIGIN
    )
    assert r.status_code == 200, r.text

    done = (await house.audio.speak_reply(turn_id))[-1]
    assert done.data.clip == "approval.done"
    assert house.ha_calls() == [("lock", "unlock", ["lock.front_door"])]

    calls = await house.tool_calls(turn_id)
    assert [(c.tool, c.decision) for c in calls] == [
        ("home.turn_off", "confirm"),
        ("home.turn_off", "allow"),
    ]
    assert calls[-1].approval_id == approval_id

    # Single use: a second tap is refused and nothing runs again.
    r = await house.web(house.alex).post(
        f"/api/approvals/{approval_id}/approve", json={"nonce": nonce}, headers=SAME_ORIGIN
    )
    assert r.status_code == 409
    assert len(house.ha.calls) == 1


async def test_reconnect_resyncs_and_web_chat_stays_off_the_device(house: House) -> None:
    house.llm(text("Hello from the page."))
    r = await house.web(house.alex).post(
        "/api/chat", json={"text": "say hello"}, headers=SAME_ORIGIN
    )
    assert r.status_code == 200, r.text
    assert r.json()["reply"] == "Hello from the page."
    await asyncio.sleep(0.1)
    assert not any(isinstance(e, SpeakEvent) for e in house.audio.log)

    # Audio restarts: core reconnects with backoff and sends a fresh profiles.sync.
    channel = house.audio.channel
    assert channel is not None
    house.audio.connected.clear()
    await channel.close()
    await asyncio.wait_for(house.audio.connected.wait(), WAIT_S)
    again: ProfilesSyncEvent = await house.audio.next(ProfilesSyncEvent)
    assert house.sync is not None
    assert {p.profile_id for p in again.data.profiles} == {
        p.profile_id for p in house.sync.data.profiles
    }
    assert house.service.audio is not None
    assert house.service.audio.connections == 2


async def test_offline_fast_path_commands_and_the_offline_phrase(house: House) -> None:
    """ROADMAP v0.1 #6 on x86: with no LLM reachable the three commands still complete on
    the fast path and "what's the weather" gets the ``offline`` phrase. Timing is the Pi's."""
    house.service.loop.provider = NoLLM("offline")
    for words, tool in (
        ("set a timer for ten minutes", "timer.set"),
        ("add milk to the shopping list", "list.add"),
        ("turn off the kitchen lights", "home.turn_off"),
    ):
        turn_id = await house.audio.say(words, profile_id=house.alex)
        reply = await house.audio.speak_reply(turn_id)
        assert reply[-1].data.last
        row = await house.turn_row(turn_id)
        assert row.path == "fast", words
        calls = await house.tool_calls(turn_id)
        assert [(c.tool, c.decision, c.result_status) for c in calls] == [(tool, "allow", "ok")]

    turn_id = await house.audio.say("what's the weather", profile_id=house.alex)
    reply = await house.audio.speak_reply(turn_id)
    assert [s.data.clip for s in reply] == ["offline"]
    assert (await house.turn_row(turn_id)).path == "offline"


async def test_health_detail_reports_live_ha_and_audio(house: House) -> None:
    """OPERATIONS §11: every ``HealthReport`` field, with the live probes wired."""
    r = await house.web(house.alex).get("/api/health/detail")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(HealthReport.model_fields) <= set(body)
    assert body["ha_connected"] is True
    assert body["audio_device_present"] is True
    assert "ha" in body["modules"]
    r = await house.web(house.sam).get("/api/health/detail")
    assert r.status_code in (403, 404)  # details need an admin session
