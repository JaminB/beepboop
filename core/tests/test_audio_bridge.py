"""The audio bridge (``beepboop.audio_bridge``) over the in-process protocol fake.

Every event goes through the real codec (``beepboop_protocol.fake_pair``). Core is the
real ``CoreService`` with a passphrase KEK (so centroids and PIN hashes are sealed) and no
modules; the audio side is played by the test.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from beepboop.agent.types import Speech
from beepboop.api.enroll import CONSENT_VERSION
from beepboop.api.people import create_profile
from beepboop.api.security import DekColumnSealer, column_aad
from beepboop.audio_bridge import (
    AudioBridge,
    DbPinStore,
    centroids_for,
    hotwords_for,
    pack_centroids,
    unpack_centroids,
)
from beepboop.db import repo
from beepboop.policy.pin import PinState, hash_pin
from beepboop.service import CoreConfig, CoreService
from beepboop_protocol import (
    PROTOCOL_MAJOR,
    AudioHelloData,
    AudioHelloEvent,
    AudioSide,
    CoreEvent,
    DeviceInfo,
    EnrollClipData,
    EnrollClipEvent,
    EnrollQuality,
    EnrollStartEvent,
    HealthData,
    HealthEvent,
    ProfilesSyncEvent,
    SpeakerInfo,
    SpeakEvent,
    SttInfo,
    TtsInfo,
    WakeInfo,
    fake_pair,
)

DIM = 4
MODEL = "fake-embedder"


def _hello() -> AudioHelloEvent:
    return AudioHelloEvent(
        data=AudioHelloData(
            protocol=PROTOCOL_MAJOR,
            device=DeviceInfo(id="main", name="Kitchen"),
            wake=WakeInfo(provider="fake-wake", models=["hey_beepboop"]),
            stt=SttInfo(provider="fake-stt", locale="en-US"),
            tts=TtsInfo(provider="fake-tts", voices=["en_US-lessac-low"]),
            speaker=SpeakerInfo(model_id=MODEL, model_sha="0" * 16, dim=DIM),
            aec_verified=False,
        )
    )


@dataclass
class Rig:
    service: CoreService
    bridge: AudioBridge
    audio: AudioSide
    alex: str
    task: asyncio.Task[None]

    async def next(self, kind: type[CoreEvent], wait_s: float = 5.0) -> CoreEvent:
        async with asyncio.timeout(wait_s):
            while True:
                event = await self.audio.receive()
                if isinstance(event, kind):
                    return event


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    passphrase = tmp_path / "kek.passphrase"
    passphrase.write_text("test passphrase\n", encoding="utf-8")
    modules = tmp_path / "modules"
    modules.mkdir()
    config = CoreConfig(
        data_dir=tmp_path / "data", modules_dir=modules, kek_passphrase_file=passphrase
    )
    service = await CoreService.start(config)
    bridge = service.audio
    assert bridge is not None
    alex = await service.db.write(
        lambda c: create_profile(c, "Alex", "admin", created_by=None).id, durable=True
    )
    audio, core = fake_pair()
    task = asyncio.create_task(bridge.handle(core, _hello()))
    try:
        yield Rig(service, bridge, audio, alex, task)
    finally:
        await audio.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, 5)
        await service.stop()


# --------------------------------------------------------------- centroids --


def test_centroid_blob_roundtrip_and_shape_check() -> None:
    rows = [[0.5, -0.25, 1.0, 0.0], [1.0, 2.0, 3.0, 4.0]]
    assert unpack_centroids(pack_centroids(rows), 2, 4) == rows
    with pytest.raises(ValueError, match="k x dim"):
        unpack_centroids(pack_centroids(rows), 3, 4)


vectors = st.lists(
    st.lists(st.floats(-1, 1, allow_nan=False, width=32), min_size=DIM, max_size=DIM).filter(
        lambda v: math.sqrt(sum(x * x for x in v)) > 1e-3
    ),
    min_size=1,
    max_size=16,
)


@settings(max_examples=60, deadline=None)
@given(vectors)
def test_centroids_are_few_unit_and_deterministic(embeddings: list[list[float]]) -> None:
    out = centroids_for(embeddings)
    assert 1 <= len(out) <= min(4, max(1, len(embeddings) // 3))
    for c in out:
        assert math.isclose(math.sqrt(sum(x * x for x in c)), 1.0, rel_tol=1e-6)
    assert centroids_for(embeddings) == out


def test_two_separate_voices_get_two_centroids() -> None:
    a = [[1.0, 0.01 * i, 0.0, 0.0] for i in range(6)]
    b = [[0.0, 0.0, 1.0, 0.01 * i] for i in range(6)]
    assert len(centroids_for(a + b)) == 2
    assert len(centroids_for(a)) == 1


def test_hotwords_are_capped_lowercased_and_short() -> None:
    from beepboop.db.repo.profiles import ProfileRow

    people = [ProfileRow(f"p{i}", f"Person {i}", "member", "act") for i in range(250)]
    people.append(ProfileRow("x", "a name that is far too long", "member", "act"))
    household, personal = hotwords_for(people, None)
    assert len(household) == 200
    assert household[0] == "person 0"
    assert "a name that is far too long" not in household
    assert personal == {}


# ------------------------------------------------------------------ bridge --


async def test_connect_sends_profiles_sync_with_guest_vocabulary(rig: Rig) -> None:
    sync = await rig.next(ProfilesSyncEvent)
    assert isinstance(sync, ProfilesSyncEvent)
    rows = {p.profile_id: p for p in sync.data.profiles}
    assert sync.data.model_id == MODEL
    assert rows["guest"].centroids == []
    assert "alex" in rows["guest"].hotwords
    assert rows[rig.alex].centroids == []
    assert await rig.bridge.audio_device_present() is True
    await rig.audio.send(
        HealthEvent(
            data=HealthData(
                cpu_temp_c=50.0,
                throttled=False,
                stt_rtf=0.2,
                playback_queue_ms=0,
                models_loaded=False,
                aec_verified=False,
            )
        )
    )
    async with asyncio.timeout(5):
        while rig.bridge.health is None:  # noqa: ASYNC110 - the reader task sets it
            await asyncio.sleep(0.01)
    assert await rig.bridge.audio_device_present() is False


async def test_enrollment_becomes_a_sealed_voiceprint_and_a_new_sync(rig: Rig) -> None:
    await rig.next(ProfilesSyncEvent)
    hub = rig.service.api.enroll_hub if rig.service.api else None
    assert hub is not None
    state = hub.start(rig.alex, "admin", consent_version=CONSENT_VERSION, consented_by=None)
    await rig.bridge.start(state.session_id, rig.alex, state.prompts())
    start = await rig.next(EnrollStartEvent)
    assert isinstance(start, EnrollStartEvent)
    assert start.data.mode == "free"  # a mixed plan: one session, free mode
    for i in range(state.total):
        clip = EnrollClipData(
            session_id=state.session_id,
            index=i,
            accepted=True,
            quality=EnrollQuality(snr_db=30.0, clipping=0.0, speech_ratio=0.8, duration_ms=1500),
            embedding=[1.0, 0.0, 0.01 * (i % 3), 0.0],
        )
        await rig.audio.send(EnrollClipEvent(data=clip))
    async with asyncio.timeout(5):
        while not state.done:  # noqa: ASYNC110 - the bridge feeds the hub
            await asyncio.sleep(0.01)
    await hub.finish(state.session_id)

    sync = await rig.next(ProfilesSyncEvent)
    assert isinstance(sync, ProfilesSyncEvent)
    row = next(p for p in sync.data.profiles if p.profile_id == rig.alex)
    assert len(row.centroids) == 1
    assert row.centroids[0][0] == pytest.approx(1.0, abs=1e-3)

    stored = await rig.service.db.read(lambda c: repo.voiceprints.get(c, rig.alex, MODEL))
    assert stored is not None
    assert pack_centroids(row.centroids) not in stored.centroids_enc  # sealed at rest
    n = await rig.service.db.read(lambda c: repo.voiceprints.count_samples(c, rig.alex, "enroll"))
    assert n == state.total


async def test_speak_numbering_one_utterance_per_reply(rig: Rig) -> None:
    await rig.next(ProfilesSyncEvent)
    turn = "01JBX6Q8Y4H3K9ZR2M7V5T8WDA"
    await rig.bridge.speak(turn, Speech(text="First."), last=False)
    await rig.bridge.speak(turn, Speech(text="Second?", listen_after=True), last=True)
    await rig.bridge.speak(turn, Speech(clip="approval.done"), last=True)
    await rig.bridge.speak(turn, Speech(text="   "), last=True)  # blank: nothing sent
    got = [await rig.next(SpeakEvent) for _ in range(3)]
    spoken = [e for e in got if isinstance(e, SpeakEvent)]
    assert [(e.data.utterance_id, e.data.seq, e.data.last) for e in spoken] == [
        (f"{turn}.0", 0, False),
        (f"{turn}.0", 1, True),
        (f"{turn}.1", 0, True),
    ]
    assert spoken[1].data.listen_after
    assert not spoken[0].data.listen_after


async def test_announce_speaks_and_records_a_system_turn(rig: Rig) -> None:
    await rig.next(ProfilesSyncEvent)
    await rig.bridge.announce("Your pasta timer is done.", profile_id=rig.alex)
    event = await rig.next(SpeakEvent)
    assert isinstance(event, SpeakEvent)
    assert event.data.text == "Your pasta timer is done."
    assert event.turn_id is not None
    row = await rig.service.db.read(lambda c: repo.turns.get_turn(c, event.turn_id or ""))
    assert row is not None
    assert (row.via, row.profile_id, row.reply) == ("system", rig.alex, event.data.text)


async def test_claim_code_is_spoken_and_repeats_until_used(rig: Rig) -> None:
    await rig.next(ProfilesSyncEvent)
    rig.bridge.claim_repeat_s = 0.05
    rig.bridge.ca_fingerprint = lambda: "3A 7F 09 C2 1B E4"
    claim = rig.bridge.claim
    assert claim is not None
    code = claim.issue(datetime.now(UTC))
    first = await rig.next(SpeakEvent)
    fingerprint = await rig.next(SpeakEvent)
    second = await rig.next(SpeakEvent)
    for e in (first, second):
        assert isinstance(e, SpeakEvent)
        assert e.data.clip == "claim.code"
        assert e.data.text == f"Your setup code is {' '.join(code)}."
    # WEB_UI §5.1 / §7: the CA fingerprint follows the code so it can be checked out of band.
    assert isinstance(fingerprint, SpeakEvent)
    assert fingerprint.data.clip == "claim.fingerprint"
    assert fingerprint.data.text == "My certificate fingerprint starts 3A 7F 09 C2 1B E4."
    assert claim.check(code, datetime.now(UTC))[0] == "ok"
    await asyncio.sleep(0.15)
    task = rig.bridge._claim_task  # pyright: ignore[reportPrivateUsage] - the repeat loop
    assert task is not None
    assert task.done()


async def test_offline_bridge_drops_speech_and_refuses_enrollment(tmp_path: Path) -> None:
    modules = tmp_path / "modules"
    modules.mkdir()
    service = await CoreService.start(CoreConfig(data_dir=tmp_path / "d", modules_dir=modules))
    try:
        bridge = service.audio
        assert bridge is not None
        assert await bridge.audio_device_present() is None  # never connected: unknown
        assert not bridge.available
        await bridge.speak("01JBX6Q8Y4H3K9ZR2M7V5T8WDA", Speech(text="hi"), last=True)
        await bridge.announce("nobody hears this")
        with pytest.raises(RuntimeError, match="audio_offline"):
            await bridge.start("s1", "01JBW2K3M4N5P6Q7R8S9T0VWXY", ["Hey beepboop"])
    finally:
        await service.stop()


# --------------------------------------------------------------- PIN store --


async def test_pin_store_unseals_counts_and_clears(rig: Rig) -> None:
    db = rig.service.db
    sealer = rig.service.api.sealer if rig.service.api else None
    assert isinstance(sealer, DekColumnSealer)
    pin_hash = hash_pin("4821")
    blob = await sealer.seal(column_aad("profiles", rig.alex, "pin_hash_enc"), pin_hash.encode())
    await db.write(lambda c: repo.profiles.set_pin(c, rig.alex, blob), durable=True)
    refreshed: list[bool] = []

    async def on_disabled() -> None:
        refreshed.append(True)

    store = DbPinStore(db, sealer, on_disabled=on_disabled)
    got, state = await store.load(rig.alex)
    assert got == pin_hash
    assert state.enabled
    assert state.failures == 0

    locked = datetime.now(UTC) + timedelta(minutes=5)
    await store.save(rig.alex, PinState(enabled=True, failures=3, locked_until=locked))
    _, state = await store.load(rig.alex)
    assert state.failures == 3
    assert state.locked_until is not None
    assert state.is_locked(datetime.now(UTC))

    await store.save(rig.alex, PinState(enabled=False, failures=10))
    got, state = await store.load(rig.alex)
    assert got is None
    assert not state.enabled
    assert refreshed == [True]

    def pin_column(c: sqlite3.Connection) -> object:
        return c.execute("SELECT pin_hash_enc FROM profiles WHERE id = ?", (rig.alex,)).fetchone()[
            0
        ]

    assert await db.read(pin_column) is None
