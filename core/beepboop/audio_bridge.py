"""The audio bridge: core's end of the event socket (ARCHITECTURE §4, §5; core/README.md).

Core **connects** to the audio service (``CoreEventClient``, backoff 0.5 s → 5 s) and runs
:meth:`AudioBridge.handle` on every connection. The bridge is the one object that turns
the protocol into core calls and back:

* **turns**: every audio event goes to a fresh :class:`~beepboop.agent.voice.VoiceSession`
  per connection (wake → Principal → ``TurnLoop``); the bridge is that loop's
  :class:`~beepboop.agent.types.Speaker` (``speak`` / ``earcon``) and
  :class:`~beepboop.agent.types.VoiceControl` (``cancel`` / ``capture_pin``).
* **voiceprints**: ``profiles.sync`` on connect and after every voiceprint, voice or
  vocabulary change (:func:`build_profiles_sync`), decrypting centroids only to send them
  (VOICE_PIPELINE §9.4). The household hotword list rides on the ``guest`` row (§10.2).
* **enrollment**: :class:`~beepboop.api.ports.EnrollmentChannel` (``enroll.start`` /
  ``enroll.stop``) and ``enroll.clip`` → :meth:`EnrollmentHub.clip`; the finished
  session's embeddings become a voiceprint through :class:`DbVoiceprintSink`.
* **speech without a turn**: the claim code (WEB_UI §5.1, OPERATIONS §6: at once, then
  every 60 s for 5 minutes while unclaimed) and ``ctx.announce`` (MODULES §9), each under a
  core-minted ``turn_id`` with a ``turns`` row ``via: system`` for announcements.
* **spoken PIN**: :class:`DbPinStore` unseals ``profiles.pin_hash_enc`` only to put it in
  ``capture_pin`` and keeps the §8.4 lockout counters on the profile row.
* **health**: the last ``health`` / ``mic.state`` for the health page.

No audio ever reaches this module (hard rule 4): only the text events of §4.2.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import sqlite3
import struct
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import cast

from beepboop.agent import phrases
from beepboop.agent.turn import TurnLoop
from beepboop.agent.types import CancelReason, Speech
from beepboop.agent.voice import VoiceSession
from beepboop.api.claim import ClaimManager, ClaimMode
from beepboop.api.enroll import EnrollmentHub
from beepboop.api.security import ColumnSealer, column_aad
from beepboop.db import Database, repo
from beepboop.db.ids import from_ms, new_ulid, now_ms, to_ms
from beepboop.db.repo.policy_view import household_calibration
from beepboop.db.repo.profiles import ProfileRow
from beepboop.db.repo.voiceprints import DEFAULT_CALIB, VoiceprintRow
from beepboop.log import get_logger
from beepboop.module.types import AnnouncePriority
from beepboop.policy.pin import PinState
from beepboop.policy.types import ALL_SCOPES, GUEST_PROFILE_ID, Principal
from beepboop.registry.resolver import EntityIndex
from beepboop_protocol import (
    AudioEvent,
    AudioHelloEvent,
    Calibration,
    CancelData,
    CancelEvent,
    CapturePinData,
    CapturePinEvent,
    CoreEvent,
    CoreSide,
    EarconData,
    EarconEvent,
    EnrollClipEvent,
    EnrollStartData,
    EnrollStartEvent,
    EnrollStopData,
    EnrollStopEvent,
    ErrorEvent,
    HealthData,
    HealthEvent,
    MicStateEvent,
    MuteData,
    MuteEvent,
    ProfilesSyncData,
    ProfilesSyncEvent,
    ProfileVoiceprint,
    SpeakData,
    SpeakDoneEvent,
    SpeakerFinalEvent,
    SpeakEvent,
    TierThresholds,
    VoiceSpec,
)

log = get_logger(__name__)

#: VOICE_PIPELINE §11.1 default voice when a profile has none (audio maps unknown ids too).
DEFAULT_VOICE = "en_US-lessac-low"
#: VOICE_PIPELINE §10.2: at most 200 hotwords per list, phrases of at most 4 words.
MAX_HOTWORDS = 200
MAX_HOTWORD_WORDS = 4
#: OPERATIONS §6: the claim code repeats every 60 s for 5 minutes.
CLAIM_REPEAT_S = 60.0
CLAIM_REPEAT_FOR_S = 300.0
#: VOICE_PIPELINE §8.2: k ≤ 4 centroids per profile; when a split counts (targets).
MAX_CENTROIDS = 4
SPLIT_SILHOUETTE = 0.1
SPLIT_MAX_COSINE = 0.9
WAKE_PHRASE = "Hey beepboop"
#: ``profiles.sync.cohort_sha`` until a bundled cohort exists (VOICE_PIPELINE §8.3).
NO_COHORT = "none"


# ------------------------------------------------------------------ centroids --


def pack_centroids(centroids: Sequence[Sequence[float]]) -> bytes:
    """``k * dim`` float32, little-endian (DATA_AND_PRIVACY §6.1 ``centroids_enc`` plaintext)."""
    flat = [float(x) for c in centroids for x in c]
    return struct.pack(f"<{len(flat)}f", *flat)


def unpack_centroids(blob: bytes, k: int, dim: int) -> list[list[float]]:
    if k < 0 or dim <= 0 or len(blob) != 4 * k * dim:
        msg = "centroid blob does not match k x dim"
        raise ValueError(msg)
    flat = struct.unpack(f"<{k * dim}f", blob)
    return [list(flat[i * dim : (i + 1) * dim]) for i in range(k)]


def _unit(v: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v] if norm > 0 else list(v)


def _cos(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def _mean_unit(rows: Sequence[Sequence[float]]) -> list[float]:
    dim = len(rows[0])
    return _unit([sum(r[i] for r in rows) / len(rows) for i in range(dim)])


def _kmeans(points: list[list[float]], k: int, iters: int = 20) -> list[int]:
    """Spherical k-means, deterministic farthest-point seeding. Returns labels."""
    seeds = [0]
    while len(seeds) < k:
        far = min(range(len(points)), key=lambda i: max(_cos(points[i], points[s]) for s in seeds))
        seeds.append(far)
    centers = [points[s] for s in seeds]
    labels = [0] * len(points)
    for _ in range(iters):
        new = [max(range(k), key=lambda c, p=p: _cos(p, centers[c])) for p in points]
        if new == labels and _ > 0:
            break
        labels = new
        for c in range(k):
            members = [p for p, lab in zip(points, labels, strict=True) if lab == c]
            if members:
                centers[c] = _mean_unit(members)
    return labels


def _silhouette(points: list[list[float]], labels: list[int], k: int) -> float:
    total = 0.0
    for i, p in enumerate(points):
        dist: dict[int, list[float]] = {}
        for j, q in enumerate(points):
            if i != j:
                dist.setdefault(labels[j], []).append(1.0 - _cos(p, q))
        own = dist.get(labels[i], [])
        if not own:
            continue  # a singleton contributes 0
        a = sum(own) / len(own)
        others = [sum(d) / len(d) for c, d in dist.items() if c != labels[i] and d]
        if not others:
            continue
        b = min(others)
        total += (b - a) / max(a, b) if max(a, b) > 0 else 0.0
    return total / len(points)


def centroids_for(embeddings: Sequence[Sequence[float]]) -> list[list[float]]:
    """VOICE_PIPELINE §8.2: ``k ≤ 4`` unit centroids by k-means, ``k`` by silhouette.

    A split is kept only if its silhouette beats ``SPLIT_SILHOUETTE`` (0.1, target), every
    cluster holds at least 3 clips, and no two centroids are closer than
    ``SPLIT_MAX_COSINE`` (0.9, target); otherwise ``k = 1`` (one voice, one centroid).
    """
    points = [_unit(e) for e in embeddings]
    if not points:
        msg = "no embeddings"
        raise ValueError(msg)
    best_labels, best_k, best_score = [0] * len(points), 1, SPLIT_SILHOUETTE
    for k in range(2, min(MAX_CENTROIDS, len(points) // 3) + 1):
        labels = _kmeans(points, k)
        sizes = [labels.count(c) for c in range(k)]
        if min(sizes) < 3:
            continue
        centers = [_cluster_mean(points, labels, c) for c in range(k)]
        closest = max(_cos(centers[i], centers[j]) for i in range(k) for j in range(i + 1, k))
        if closest > SPLIT_MAX_COSINE:
            continue
        score = _silhouette(points, labels, k)
        if score > best_score:
            best_labels, best_k, best_score = labels, k, score
    return [_cluster_mean(points, best_labels, c) for c in range(best_k)]


def _cluster_mean(points: list[list[float]], labels: list[int], c: int) -> list[float]:
    return _mean_unit([p for p, lab in zip(points, labels, strict=True) if lab == c])


# ------------------------------------------------------------------ hotwords --


def _phrase_ok(text: str) -> str | None:
    t = " ".join(text.lower().split())
    return t if t and len(t.split()) <= MAX_HOTWORD_WORDS else None


def _capped(words: Sequence[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for w in words:
        ok = _phrase_ok(w)
        if ok is not None and ok not in seen:
            seen.add(ok)
            out.append(ok)
        if len(out) >= MAX_HOTWORDS:
            break
    return out


def hotwords_for(
    profiles: Sequence[ProfileRow], index: EntityIndex | None
) -> tuple[list[str], dict[str, list[str]]]:
    """(household list, per-profile lists) for ``profiles.sync`` (VOICE_PIPELINE §10.2).

    v0.1 sources: household member names and HA area names (household); the names and
    aliases of the HA entities each profile can reach (per profile). List names, timer
    labels and module vocabulary are not collected yet.
    """
    household = [p.name for p in profiles if p.kind != "guest"]
    personal: dict[str, list[str]] = {}
    if index is not None:
        for area in index.areas():
            household += [area.name, *area.aliases]
        for p in profiles:
            principal = Principal(p.id, "voice", 1.0, 1.0, "main", ALL_SCOPES, False, "sync")
            names: list[str] = []
            for e in index.visible(principal):
                names += [e.name, *e.aliases]
            personal[p.id] = _capped(names)
    return _capped(household), personal


async def build_profiles_sync(
    db: Database,
    sealer: ColumnSealer | None,
    *,
    model_id: str,
    dim: int,
    default_voice: str = DEFAULT_VOICE,
    index: EntityIndex | None = None,
) -> ProfilesSyncData:
    """The full ``profiles.sync`` snapshot for one connection's speaker model.

    Every enabled profile gets a row (voice, rate, locale, hotwords); centroids only for a
    voiceprint of this ``model_id`` and ``dim`` that the sealer can open. ``guest`` carries
    the household hotwords and never centroids (DATA_AND_PRIVACY §8).
    """

    def read(
        c: sqlite3.Connection,
    ) -> tuple[list[ProfileRow], list[VoiceprintRow], dict[str, object]]:
        base = household_calibration(c)  # System > Voice thresholds as the default
        defaults: dict[str, object] = {
            "tau": {"act": base.tau_act, "personal": base.tau_personal},
            "margin": {"act": base.margin_act, "personal": base.margin_personal},
            "asnorm": {},
        }
        return repo.profiles.list_(c), repo.voiceprints.list_for_model(c, model_id), defaults

    profiles, prints, household_calib = await db.read(read)
    active = [p for p in profiles if p.disabled_at is None]
    by_profile = {v.profile_id: v for v in prints if v.dim == dim}
    household, personal = hotwords_for(active, index)
    rows: list[ProfileVoiceprint] = []
    for p in active:
        centroids: list[list[float]] = []
        calib: dict[str, object] = dict(household_calib)
        vp = by_profile.get(p.id) if p.id != GUEST_PROFILE_ID else None
        if vp is not None and sealer is not None:
            try:
                plain = await sealer.open(
                    column_aad("voiceprints", p.id, "centroids_enc"), vp.centroids_enc
                )
                centroids = unpack_centroids(plain, vp.k, vp.dim)
                calib = vp.calib
            except Exception as exc:
                log.warning("voiceprint.unreadable", profile_id=p.id, error=type(exc).__name__)
        rows.append(
            ProfileVoiceprint(
                profile_id=p.id,
                centroids=centroids,
                calib=_calibration(calib),
                hotwords=household if p.id == GUEST_PROFILE_ID else personal.get(p.id, []),
                voice=VoiceSpec(voice_id=p.tts_voice or default_voice, rate=_rate(p.speech_rate)),
                locale=p.locale,
            )
        )
    return ProfilesSyncData(profiles=rows, cohort_sha=NO_COHORT, model_id=model_id)


def _rate(rate: float) -> float:
    return min(4.0, max(0.25, rate))


def _calibration(raw: dict[str, object]) -> Calibration:
    """``calib_json`` → wire thresholds; a missing or malformed value takes the default."""

    def pair(key: str) -> TierThresholds:
        merged: dict[str, object] = {}
        for source in (DEFAULT_CALIB.get(key), raw.get(key)):
            if isinstance(source, dict):
                merged.update(cast(dict[str, object], source))
        return TierThresholds.model_validate({"act": merged["act"], "personal": merged["personal"]})

    return Calibration(tau=pair("tau"), margin=pair("margin"))


# ------------------------------------------------------------------ stores --


@dataclass
class DbVoiceprintSink:
    """``VoiceprintSink``: enrollment embeddings → sealed centroids + anchor samples."""

    db: Database
    sealer: ColumnSealer
    model: Callable[[], tuple[str, str, int] | None]
    on_saved: Callable[[], object] = lambda: None

    async def save(
        self,
        profile_id: str,
        embeddings: Sequence[Sequence[float]],
        *,
        consent_version: str,
        consented_by: str | None,
    ) -> None:
        model = self.model()
        if model is None:
            msg = "audio_offline"
            raise RuntimeError(msg)
        model_id, model_sha, dim = model
        if not embeddings or any(len(e) != dim for e in embeddings):
            msg = "embedding length does not match the speaker model"
            raise ValueError(msg)
        centroids = centroids_for(embeddings)
        sealed = await self.sealer.seal(
            column_aad("voiceprints", profile_id, "centroids_enc"), pack_centroids(centroids)
        )
        samples: list[tuple[str, bytes]] = []
        for e in embeddings:
            sid = new_ulid()
            blob = await self.sealer.seal(
                column_aad("voice_samples", sid, "emb_enc"), pack_centroids([e])
            )
            samples.append((sid, blob))
        row = VoiceprintRow(
            profile_id=profile_id,
            model_id=model_id,
            model_sha=model_sha,
            dim=dim,
            centroids_enc=sealed,
            k=len(centroids),
            n_clips=len(embeddings),
            calib=dict(DEFAULT_CALIB),
            consent_at=now_ms(),
            consent_version=consent_version,
            consented_by=consented_by,
        )

        def write(c: sqlite3.Connection) -> None:
            c.execute(
                "DELETE FROM voice_samples WHERE profile_id = ? AND kind = 'enroll'",
                (profile_id,),
            )
            repo.voiceprints.upsert(c, row)
            for sid, blob in samples:
                repo.voiceprints.insert_sample(
                    c, profile_id, model_id, "enroll", blob, sample_id=sid
                )

        await self.db.write(write, durable=True)
        result = self.on_saved()
        if asyncio.iscoroutine(result):
            await result


@dataclass
class DbPinStore:
    """``PinStore`` over ``profiles.pin_*`` (IDENTITY_AND_TRUST §8.4).

    The hash is unsealed only to go into ``capture_pin``. ``pin_failures`` and
    ``pin_locked_until`` persist; the 15-minute and 24-hour window starts are kept in
    memory (a restart restarts the windows, never unlocks a lock). Ten mismatches in 24 h
    clear the PIN (the person sets a new one in the web UI).
    """

    db: Database
    sealer: ColumnSealer | None
    #: Rebuild the policy snapshot when a PIN is cleared (``pin_enabled`` changes).
    on_disabled: Callable[[], Awaitable[None]] | None = None
    _windows: dict[str, PinState] = field(default_factory=dict[str, PinState])

    async def load(self, profile_id: str) -> tuple[str | None, PinState]:
        row = await self.db.read(lambda c: repo.profiles.get(c, profile_id))
        if row is None or row.pin_hash_enc is None or self.sealer is None:
            return None, PinState(enabled=False)
        try:
            raw = await self.sealer.open(
                column_aad("profiles", profile_id, "pin_hash_enc"), row.pin_hash_enc
            )
        except Exception as exc:
            log.warning("pin.unreadable", profile_id=profile_id, error=type(exc).__name__)
            return None, PinState(enabled=False)
        mem = self._windows.get(profile_id, PinState(enabled=True))
        state = replace(
            mem,
            enabled=True,
            failures=row.pin_failures,
            locked_until=None if row.pin_locked_until is None else from_ms(row.pin_locked_until),
        )
        return raw.decode(), state

    async def save(self, profile_id: str, state: PinState) -> None:
        self._windows[profile_id] = state
        locked = None if state.locked_until is None else to_ms(state.locked_until)

        def write(c: sqlite3.Connection) -> None:
            row = repo.profiles.get(c, profile_id)
            if row is None:
                return
            if not state.enabled:
                repo.profiles.set_pin(c, profile_id, None)
                row = repo.profiles.get(c, profile_id) or row
            repo.profiles.update(c, repo.profiles.with_pin_failures(row, state.failures, locked))

        await self.db.write(write, durable=True)
        if not state.enabled and self.on_disabled is not None:
            await self.on_disabled()


# ------------------------------------------------------------------ bridge --


@dataclass
class _Utterances:
    """Per-turn ``speak`` numbering: one ``utterance_id`` per reply, ``seq`` within it."""

    index: int = 0
    seq: int = 0


@dataclass
class AudioBridge:
    """Speaker + VoiceControl + EnrollmentChannel over one live audio connection."""

    db: Database
    sealer: ColumnSealer | None = None
    enroll_hub: EnrollmentHub | None = None
    entity_index: Callable[[], EntityIndex | None] = lambda: None
    claim: ClaimManager | None = None
    default_voice: str = DEFAULT_VOICE
    default_locale: str = "en-US"
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    claim_repeat_s: float = CLAIM_REPEAT_S
    claim_repeat_for_s: float = CLAIM_REPEAT_FOR_S
    #: The Tier-0 CA fingerprint ("3A 7F 09 C2 1B E4"), spoken right after the claim code
    #: so the person can compare it with ``/ca`` out of band (WEB_UI §5.1, §7).
    ca_fingerprint: Callable[[], str | None] = lambda: None
    loop: TurnLoop | None = None
    channel: CoreSide | None = None
    hello: AudioHelloEvent | None = None
    session: VoiceSession | None = None
    health: HealthData | None = None
    muted: bool = False
    connections: int = 0
    _speak: dict[str, _Utterances] = field(default_factory=dict[str, _Utterances])
    _turn_profile: dict[str, str] = field(default_factory=dict[str, str])
    _profiles: dict[str, ProfileRow] = field(default_factory=dict[str, ProfileRow])
    _claim_task: asyncio.Task[None] | None = None
    _tasks: set[asyncio.Task[None]] = field(default_factory=set[asyncio.Task[None]])
    #: Every event received, in order (type and turn only; tests and the debug log).
    received: list[tuple[str, str | None]] = field(default_factory=list[tuple[str, str | None]])

    def attach(self, loop: TurnLoop) -> None:
        """Become ``loop``'s speaker and voice control (done once, at start)."""
        self.loop = loop
        loop.speaker = self
        loop.control = self

    # ------------------------------------------------------------- connection --

    @property
    def connected(self) -> bool:
        return self.channel is not None

    async def handle(self, channel: CoreSide, hello: AudioHelloEvent) -> None:
        """``CoreEventClient`` handler: one connection, until it closes."""
        if self.loop is None:
            msg = "AudioBridge.attach(loop) first"
            raise RuntimeError(msg)
        session = VoiceSession(self.loop, control=self, tts_provider=hello.data.tts.provider)
        self.channel, self.hello, self.session = channel, hello, session
        self.connections += 1
        log.info("audio.connected", device=hello.data.device.id, dim=hello.data.speaker.dim)
        try:
            await self.sync_profiles()
            if self.claim is not None and self.claim.mode(self.clock()) is not None:
                self._start_claim_repeats()
            async for event in channel:
                await self._on_event(event)
        finally:
            await session.close()
            if self.channel is channel:
                self.channel, self.session = None, None
            self._speak.clear()
            self._turn_profile.clear()
            log.info("audio.disconnected")

    async def _on_event(self, event: AudioEvent) -> None:
        self.received.append((event.type, event.turn_id))
        if isinstance(event, SpeakerFinalEvent) and event.turn_id:
            self._turn_profile[event.turn_id] = event.data.profile_id
        elif isinstance(event, EnrollClipEvent):
            if self.enroll_hub is not None:
                self.enroll_hub.clip(event.data)
            return
        elif isinstance(event, HealthEvent):
            self.health = event.data
            return
        elif isinstance(event, MicStateEvent):
            self.muted = event.data.muted
        elif isinstance(event, SpeakDoneEvent):
            return
        elif isinstance(event, ErrorEvent):
            log.warning("audio.error", code=event.data.code, fatal=event.data.fatal)
            return
        if self.session is not None:
            await self.session.on_event(event)

    async def _send(self, event: CoreEvent) -> bool:
        channel = self.channel
        if channel is None:
            log.info("audio.send_dropped", type=event.type, turn_id=event.turn_id)
            return False
        try:
            await channel.send(event)
        except Exception as exc:
            log.warning("audio.send_failed", type=event.type, error=type(exc).__name__)
            return False
        return True

    async def _profile_rows(self, *, force: bool = False) -> dict[str, ProfileRow]:
        if force or not self._profiles:
            rows = await self.db.read(repo.profiles.list_)
            self._profiles = {p.id: p for p in rows}
        return self._profiles

    async def sync_profiles(self) -> bool:
        """Send ``profiles.sync`` for this connection's model (no-op while disconnected)."""
        hello = self.hello
        if self.channel is None or hello is None:
            return False
        await self._profile_rows(force=True)
        data = await build_profiles_sync(
            self.db,
            self.sealer,
            model_id=hello.data.speaker.model_id,
            dim=hello.data.speaker.dim,
            default_voice=self.default_voice,
            index=self.entity_index(),
        )
        return await self._send(ProfilesSyncEvent(data=data))

    def speaker_model(self) -> tuple[str, str, int] | None:
        """(model_id, model_sha, dim) of the connected audio service, for the voiceprint sink."""
        if self.hello is None:
            return None
        s = self.hello.data.speaker
        return s.model_id, s.model_sha, s.dim

    # ---------------------------------------------------------------- Speaker --

    def _voice(self, turn_id: str) -> tuple[VoiceSpec, str]:
        profile = self._profiles.get(self._turn_profile.get(turn_id, GUEST_PROFILE_ID))
        if profile is None:
            return VoiceSpec(voice_id=self.default_voice, rate=1.0), self.default_locale
        return (
            VoiceSpec(
                voice_id=profile.tts_voice or self.default_voice, rate=_rate(profile.speech_rate)
            ),
            profile.locale,
        )

    async def speak(self, turn_id: str, speech: Speech, *, last: bool) -> None:
        text = speech.text.strip() if speech.text else None
        if not text and not speech.clip:
            return
        if self.channel is None:
            log.info("audio.speak_dropped", turn_id=turn_id)
            return
        n = self._speak.setdefault(turn_id, _Utterances())
        voice, locale = self._voice(turn_id)
        data = SpeakData(
            utterance_id=f"{turn_id}.{n.index}",
            seq=n.seq,
            last=last,
            text=text or None,
            clip=speech.clip,
            voice=voice,
            locale=locale,
            listen_after=speech.listen_after and last,
        )
        if last:
            n.index, n.seq = n.index + 1, 0
        else:
            n.seq += 1
        await self._send(SpeakEvent(turn_id=turn_id, data=data))

    async def earcon(self, turn_id: str, name: str) -> None:
        if self.channel is None:
            return
        await self._send(
            EarconEvent(turn_id=turn_id, data=EarconData.model_validate({"name": name}))
        )

    # ----------------------------------------------------------- VoiceControl --

    async def cancel(self, turn_id: str, reason: CancelReason) -> None:
        await self._send(CancelEvent(turn_id=turn_id, data=CancelData(reason=reason)))

    async def capture_pin(
        self, turn_id: str, approval_id: str, pin_hash: str, prompt: str | None
    ) -> None:
        await self._send(
            CapturePinEvent(
                turn_id=turn_id,
                data=CapturePinData(approval_id=approval_id, pin_hash=pin_hash, prompt=prompt),
            )
        )

    # ------------------------------------------------------ EnrollmentChannel --

    @property
    def available(self) -> bool:
        return self.connected

    async def start(self, session_id: str, profile_id: str, prompts: list[str]) -> None:
        if self.channel is None:
            msg = "audio_offline"
            raise RuntimeError(msg)
        # One mode per session: "wake" only when every prompt is the wake phrase.
        mode = "wake" if prompts and all(p == WAKE_PHRASE for p in prompts) else "free"
        sent = await self._send(
            EnrollStartEvent(
                data=EnrollStartData(
                    session_id=session_id, profile_id=profile_id, mode=mode, prompts=prompts
                )
            )
        )
        if not sent:
            msg = "audio_offline"
            raise RuntimeError(msg)

    async def stop(self, session_id: str) -> None:
        await self._send(EnrollStopEvent(data=EnrollStopData(session_id=session_id)))

    # ----------------------------------------------------- speech without a turn --

    async def say(self, speech: Speech, *, profile_id: str | None = None) -> str | None:
        """One ``speak`` under a core-minted ``turn_id``; the id, or ``None`` if offline."""
        if self.channel is None:
            return None
        turn_id = new_ulid()
        if profile_id is not None:
            self._turn_profile[turn_id] = profile_id
        await self._profile_rows()
        await self.speak(turn_id, speech, last=True)
        self._speak.pop(turn_id, None)
        self._turn_profile.pop(turn_id, None)
        return turn_id

    async def announce(
        self, text: str, *, profile_id: str | None = None, priority: AnnouncePriority = "normal"
    ) -> None:
        """``ctx.announce`` (MODULES §9): speak, and record a ``turns`` row ``via: system``."""
        if priority == "low" and self.session is not None and self.session.busy:
            log.info("announce.dropped_busy", profile_id=profile_id)
            return
        turn_id = await self.say(Speech(text=text), profile_id=profile_id)
        if turn_id is None:
            log.info("announce.dropped_offline", profile_id=profile_id, priority=priority)
            return
        tts = self.hello.data.tts.provider if self.hello is not None else "none"
        row = repo.turns.TurnRow(
            id=turn_id,
            profile_id=profile_id or GUEST_PROFILE_ID,
            via="system",
            locale=self.default_locale,
            path="fast",
            stt_provider="none",
            tts_provider=tts,
            reply=text,
        )
        try:
            await self.db.write(lambda c: repo.turns.insert_turn(c, row))
        except Exception as exc:
            log.warning("announce.record_failed", error=type(exc).__name__)

    def on_claim_code(self, code: str, mode: ClaimMode) -> None:
        """``ClaimManager.on_code``: speak the code now and every 60 s for 5 minutes."""
        del code, mode  # read again from the manager each time; never logged
        self._start_claim_repeats()

    def _start_claim_repeats(self) -> None:
        if self._claim_task is not None and not self._claim_task.done():
            self._claim_task.cancel()
        try:
            self._claim_task = asyncio.get_running_loop().create_task(self._claim_loop())
        except RuntimeError:  # no running loop (sync caller at import time)
            self._claim_task = None

    async def _claim_loop(self) -> None:
        claim = self.claim
        if claim is None:
            return
        start = self.clock()
        while claim.mode(self.clock()) is not None:
            code = claim.peek()
            if code is not None:
                digits = {f"d{i + 1}": d for i, d in enumerate(code)}
                text = phrases.render("claim.code", **digits)
                await self.say(Speech(text=text, clip="claim.code"))
                fingerprint = self.ca_fingerprint()
                groups = fingerprint.split() if fingerprint else []
                if len(groups) >= 6:
                    spoken = {f"f{i + 1}": g for i, g in enumerate(groups[:6])}
                    await self.say(
                        Speech(
                            text=phrases.render("claim.fingerprint", **spoken),
                            clip="claim.fingerprint",
                        )
                    )
            if self.clock() - start >= timedelta(seconds=self.claim_repeat_for_s):
                return
            await asyncio.sleep(self.claim_repeat_s)

    # ----------------------------------------------------------------- misc --

    async def mute(self, on: bool, until: float | None = None) -> bool:
        return await self._send(MuteEvent(data=MuteData(on=on, until=until if on else None)))

    async def audio_device_present(self) -> bool | None:
        """Health probe: ``None`` before the first connection, else connected + models."""
        if self.connections == 0:
            return None
        if self.channel is None:
            return False
        return self.health.models_loaded if self.health is not None else True

    async def close(self) -> None:
        if self._claim_task is not None:
            self._claim_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._claim_task
        if self.session is not None:
            await self.session.close()
        if self.channel is not None:
            with contextlib.suppress(Exception):
                await self.channel.close()


__all__ = [
    "DEFAULT_VOICE",
    "AudioBridge",
    "DbPinStore",
    "DbVoiceprintSink",
    "build_profiles_sync",
    "centroids_for",
    "hotwords_for",
    "pack_centroids",
    "unpack_centroids",
]
