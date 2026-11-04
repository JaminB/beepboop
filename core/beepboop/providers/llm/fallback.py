"""The fallback ladder (docs/LLM_PROVIDERS.md §3.6): fail fast and sideways.

The loop, not an adapter, owns retries and fallback. A :class:`Ladder` holds the
configured rungs (``[fast, smart, fallback]`` by default, §2.1) and streams one request:

* offline at t0 (no DNS/route to the first rung's endpoint, checked within
  ``reachability`` timeout) → :class:`Offline` at once, no request is sent;
* each rung gets ``first_event_timeout_s`` (1.2 s, target) for its first event;
  ``Throttled`` / ``Unavailable`` / that timeout → the next rung immediately (no sleep,
  no same-rung retry);
* ``BadRequest`` / ``AuthError`` → :class:`LadderSetupError`, no further rung;
* ``Unavailable`` or ``Throttled`` *after* the first event → ``Done(stop_reason="error",
  detail="lost_connection")``: what was spoken stays spoken;
* every rung failed → :class:`LadderExhausted`.

The connect timeout (1.0 s) is the adapter's client configuration (§3.6). Which rung
answered is reported through :meth:`Ladder.stream`'s ``on_rung`` callback so the turn
records it in ``turns.stages_ms_json`` / ``turns.llm_provider``.

``build_ladder`` turns the household ``llm`` document (§2.1; ``defaults.yaml`` until an
admin saves the System LLM page) and the provider secrets into rungs.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from typing import Any, Literal, Protocol, cast

import yaml
from pydantic import SecretStr

from beepboop.log import get_logger
from beepboop.providers.llm.protocol import (
    AuthError,
    BadRequest,
    Caps,
    Done,
    Effort,
    Event,
    LLMProvider,
    ProviderError,
    Request,
    SystemBlock,
    Throttled,
    Unavailable,
)

FIRST_EVENT_TIMEOUT_S = 1.2  # target, validated by G3 on the Pi (§3.6)
REACHABILITY_TIMEOUT_S = 0.5  # target; keeps "offline" inside the 1.0 s budget (§3.6)

CacheTtl = Literal["5m", "1h"]

log = get_logger(__name__)


class LadderExhausted(ProviderError):  # noqa: N818 - reads as a state, like Throttled
    """Every rung failed (or no rung could be tried)."""

    def __init__(self, message: str, attempts: Sequence[tuple[str, str]] = ()) -> None:
        super().__init__(message, code="exhausted")
        self.attempts = list(attempts)


class Offline(LadderExhausted):
    """No route to the LLM at t0: nothing was sent."""


class LadderSetupError(ProviderError):
    """``BadRequest`` / ``AuthError``: configuration is wrong; no rung is retried."""


# ---------------------------------------------------------------- reachability --


class Reachability(Protocol):
    """Is there a route to the LLM right now? Must answer within ~0.5 s."""

    async def reachable(self) -> bool: ...
    def note_failure(self) -> None: ...


class AlwaysReachable:
    async def reachable(self) -> bool:
        return True

    def note_failure(self) -> None:
        return None


@dataclass
class DnsReachability:
    """Resolve the endpoint host with a short timeout; cache the answer briefly.

    An unplugged uplink usually fails DNS at once; a hanging resolver is cut at
    ``timeout_s``. A rung failing with ``Unavailable`` marks the route down for
    ``ttl_fail_s`` so the next turns go straight to the offline phrase.
    """

    host: str
    port: int = 443
    timeout_s: float = REACHABILITY_TIMEOUT_S
    ttl_ok_s: float = 30.0
    ttl_fail_s: float = 5.0
    clock: Callable[[], float] = time.monotonic
    _cached: tuple[bool, float] | None = None

    async def reachable(self) -> bool:
        now = self.clock()
        if self._cached is not None:
            ok, until = self._cached
            if now < until:
                return ok
        try:
            await asyncio.wait_for(
                asyncio.to_thread(socket.getaddrinfo, self.host, self.port), self.timeout_s
            )
            ok = True
        except (TimeoutError, OSError):
            ok = False
        self._cached = (ok, now + (self.ttl_ok_s if ok else self.ttl_fail_s))
        return ok

    def note_failure(self) -> None:
        self._cached = (False, self.clock() + self.ttl_fail_s)


# ------------------------------------------------------------------------ ladder --


@dataclass(frozen=True, slots=True)
class Rung:
    """One alias of the ladder: a provider bound to one model, effort and cache TTL."""

    name: str
    provider: LLMProvider
    model_id: str | None = None
    effort: Effort = "low"
    cache_ttl: CacheTtl = "5m"

    @property
    def label(self) -> str:
        """``turns.llm_provider``: ``'<provider>:<model>'``."""
        return f"{self.provider.id}:{self.model_id}" if self.model_id else self.provider.id


_END = object()


def _for_rung(req: Request, rung: Rung) -> Request:
    system = [
        SystemBlock(text=b.text, cache=b.cache, ttl=rung.cache_ttl) if b.cache else b
        for b in req.system
    ]
    return req.model_copy(update={"effort": rung.effort, "system": system})


@dataclass
class Ladder:
    """An ordered list of rungs that behaves like one provider for the turn loop."""

    rungs: Sequence[Rung]
    reachability: Reachability = field(default_factory=AlwaysReachable)
    first_event_timeout_s: float = FIRST_EVENT_TIMEOUT_S
    id: str = "ladder"
    license: str = "GPL-3.0-or-later"

    @classmethod
    def single(cls, provider: LLMProvider, *, name: str = "fast") -> Ladder:
        """A one-rung ladder around a bare provider (tests, ``chat`` with a fake)."""
        model = cast(str | None, getattr(provider, "model_id", None))
        return cls([Rung(name, provider, model)])

    @property
    def caps(self) -> Caps:
        return self.rungs[0].provider.caps

    async def warm(self) -> None:
        """§3.7: warm the first rung only; never awaited on the critical path."""
        if self.rungs:
            await self.rungs[0].provider.warm()

    async def healthcheck(self) -> str:
        return await self.rungs[0].provider.healthcheck() if self.rungs else "no rungs"

    async def stream(
        self, req: Request, *, on_rung: Callable[[Rung], None] | None = None
    ) -> AsyncIterator[Event]:
        """Stream ``req`` from the first rung that answers. See the module docstring."""
        if not self.rungs:
            raise LadderExhausted("no LLM rungs configured")
        if not await self.reachability.reachable():
            raise Offline("no route to the LLM")
        attempts: list[tuple[str, str]] = []
        for rung in self.rungs:
            queue: asyncio.Queue[object] = asyncio.Queue()
            task = asyncio.create_task(_pump(rung.provider, _for_rung(req, rung), queue))
            try:
                try:
                    first = await asyncio.wait_for(queue.get(), self.first_event_timeout_s)
                except TimeoutError:
                    attempts.append((rung.name, "timeout"))
                    log.warning("llm.rung_failed", rung=rung.name, error="timeout")
                    continue
                if isinstance(first, BadRequest | AuthError):
                    raise LadderSetupError(str(first), code=first.code) from first
                if isinstance(first, Throttled | Unavailable):
                    if isinstance(first, Unavailable):
                        self.reachability.note_failure()
                    attempts.append((rung.name, type(first).__name__))
                    log.warning("llm.rung_failed", rung=rung.name, error=type(first).__name__)
                    continue
                if isinstance(first, BaseException):
                    raise first
                if first is _END:
                    attempts.append((rung.name, "empty"))
                    continue
                if on_rung is not None:
                    on_rung(rung)
                item: object = first
                while item is not _END:
                    if isinstance(item, Throttled | Unavailable):
                        yield Done(stop_reason="error", detail="lost_connection")
                        return
                    if isinstance(item, BaseException):
                        raise item
                    yield cast(Event, item)
                    item = await queue.get()
                return
            finally:
                if not task.done():
                    task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        raise LadderExhausted("every LLM rung failed", attempts)


async def _pump(provider: LLMProvider, req: Request, queue: asyncio.Queue[object]) -> None:
    try:
        async for event in provider.stream(req):
            await queue.put(event)
    except asyncio.CancelledError:
        raise
    except BaseException as exc:
        await queue.put(exc)
        return
    await queue.put(_END)


# ---------------------------------------------------------------- configuration --


@cache
def default_llm_config() -> dict[str, Any]:  # Any: a YAML document (§2.1 shape)
    text = resources.files("beepboop.providers.llm").joinpath("defaults.yaml").read_text("utf-8")
    return cast(dict[str, Any], yaml.safe_load(text))


#: Secret names a provider reads from the store (``owner_module="core"``).
BEDROCK_SECRETS = ("aws_access_key_id", "aws_secret_access_key", "bedrock_bearer_token")

ProviderFactory = Callable[
    [str, Mapping[str, Any], Mapping[str, SecretStr]], Callable[[str], LLMProvider]
]


def bedrock_factory(
    _provider_id: str, settings: Mapping[str, Any], secrets: Mapping[str, SecretStr]
) -> Callable[[str], LLMProvider]:
    """One shared Bedrock client; one ``BedrockProvider`` per model id."""
    from beepboop.providers.llm.bedrock.provider import (
        BedrockProvider,
        BedrockSettings,
        make_client,
    )

    s = BedrockSettings.from_manifest(**dict(settings))
    if s.credentials == "access_keys":
        key, secret = secrets.get("aws_access_key_id"), secrets.get("aws_secret_access_key")
        if key is None or secret is None:
            raise AuthError("credentials=access_keys needs keys", code="MissingKeys")
        client = make_client(s.region, access_key_id=key, secret_access_key=secret)
    elif s.credentials == "bearer_token":
        token = secrets.get("bedrock_bearer_token")
        if token is None or not token.get_secret_value():
            raise AuthError("credentials=bearer_token needs a token", code="MissingKeys")
        client = make_client(s.region, bearer_token=token)
    else:
        client = make_client(s.region)
    return lambda model: BedrockProvider(client, model)


def bedrock_host(settings: Mapping[str, Any]) -> str:
    from beepboop.providers.llm.bedrock.provider import BedrockSettings

    return f"bedrock-runtime.{BedrockSettings.from_manifest(**dict(settings)).region}.amazonaws.com"


def build_ladder(
    config: Mapping[str, Any] | None,
    secrets: Mapping[str, SecretStr],
    *,
    alias: str | None = None,
    factories: Mapping[str, ProviderFactory] | None = None,
    reachability: Reachability | None = None,
) -> Ladder:
    """Rungs from the household ``llm`` document. ``alias`` (a profile's ``llm.alias``)
    moves that rung to the front; the rest keep ladder order (§9: no automatic escalation).
    Raises :class:`AuthError` when the selected credentials are missing."""
    cfg = dict(config or default_llm_config())
    aliases = cast(dict[str, dict[str, Any]], cfg.get("aliases", {}))
    providers_cfg = cast(dict[str, dict[str, Any]], cfg.get("providers", {}))
    order = [str(a) for a in cast(list[Any], cfg.get("ladder", []))]
    first = alias or str(cfg.get("default_alias", order[0] if order else ""))
    if first in order:
        order.remove(first)
        order.insert(0, first)
    fac: Mapping[str, ProviderFactory] = factories or {"bedrock": bedrock_factory}
    makers: dict[str, Callable[[str], LLMProvider]] = {}
    rungs: list[Rung] = []
    for name in order:
        spec = aliases.get(name)
        if spec is None:
            continue
        pid = str(spec.get("provider", "bedrock"))
        if pid not in fac:
            log.warning("llm.unknown_provider", provider=pid, alias=name)
            continue
        if pid not in makers:
            makers[pid] = fac[pid](pid, providers_cfg.get(pid, {}) or {}, secrets)
        model = str(spec["model"])
        rungs.append(
            Rung(
                name,
                makers[pid](model),
                model,
                cast(Effort, spec.get("effort", "low")),
                cast(CacheTtl, spec.get("cache_ttl", "5m")),
            )
        )
    if reachability is None and rungs and rungs[0].provider.id == "bedrock":
        first_pid = str(aliases[rungs[0].name].get("provider", "bedrock"))
        reachability = DnsReachability(bedrock_host(providers_cfg.get(first_pid, {}) or {}))
    return Ladder(rungs, reachability or AlwaysReachable())


__all__ = [
    "BEDROCK_SECRETS",
    "FIRST_EVENT_TIMEOUT_S",
    "AlwaysReachable",
    "DnsReachability",
    "Ladder",
    "LadderExhausted",
    "LadderSetupError",
    "Offline",
    "Reachability",
    "Rung",
    "bedrock_factory",
    "build_ladder",
    "default_llm_config",
]
