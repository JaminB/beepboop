"""``beepboop serve`` as the container runs it: HTTP + SPA + the audio connection.

A fresh, unclaimed device: core starts on a free loopback port, serves the SPA and
``/api/health``, connects to the audio socket and speaks the claim code there
(OPERATIONS §6, ROADMAP v0.1 #1 up to the speech; the 15-minute install is pending on Pi).
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from pathlib import Path

import httpx

from beepboop.service import CoreConfig, serve
from beepboop_protocol import ProfilesSyncEvent, SpeakEvent

from .core_rig import REPO, WAIT_S
from .fake_audio import FakeAudio


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_serve_boots_serves_the_spa_and_speaks_the_claim_code(tmp_path: Path) -> None:
    spa = REPO / "ui" / "build"
    if not (spa / "index.html").is_file():  # CI's python job does not build the UI
        spa = tmp_path / "spa"
        spa.mkdir()
        (spa / "index.html").write_text("<!doctype html><title>beepboop</title>", "utf-8")
    audio = FakeAudio()
    address = await audio.start()
    port = _free_port()
    config = CoreConfig(
        data_dir=tmp_path / "data",
        modules_dir=REPO / "modules",
        bind_port=port,
        spa_dir=spa,
        audio_socket=f"tcp://{address.host}:{address.port}",
        ha_url="ws://127.0.0.1:9/api/websocket",  # never dialled: no HA token yet
    )
    task = asyncio.create_task(serve(config))
    try:
        await asyncio.wait_for(audio.connected.wait(), WAIT_S)
        await audio.next(ProfilesSyncEvent)
        claim: SpeakEvent = await audio.next(SpeakEvent)
        assert claim.data.clip == "claim.code"
        assert claim.data.text is not None
        assert claim.data.text.startswith("Your setup code is ")

        base = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(base_url=base) as http:
            async with asyncio.timeout(WAIT_S):
                while True:
                    try:
                        r = await http.get("/api/health")
                        break
                    except httpx.ConnectError:
                        await asyncio.sleep(0.05)
            assert r.json()["status"] == "ok"
            page = await http.get("/people")  # a client-side route: the SPA fallback
            assert page.status_code == 200
            assert "<!doctype html>" in page.text.lower()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await audio.close()
