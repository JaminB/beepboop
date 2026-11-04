"""Amazon Bedrock ConverseStream provider (docs/LLM_PROVIDERS.md §3).

boto3 is synchronous, so the provider runs the blocking parts off the event
loop: the ``converse_stream`` call in the default executor, and the event
stream iteration on a small daemon thread that hands raw events to the loop
through an ``asyncio.Queue``. Cancelling the consumer closes the HTTP stream
(the reader thread then exits) and finalises with ``Usage`` + ``Done``.

The provider only translates. It never retries (``total_max_attempts=1``), never
sleeps and never falls back; ``fallback.py`` in the loop owns that (§3.6).
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from importlib import resources
from typing import TYPE_CHECKING, Any, Literal, Protocol, Self, Unpack, cast

import yaml
from botocore.awsrequest import AWSRequest
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from botocore.tokens import FrozenAuthToken
from pydantic import BaseModel, ConfigDict, SecretStr

from beepboop.log import get_logger
from beepboop.providers.llm.bedrock.mapping import (
    StreamTranslator,
    build_converse_params,
    map_client_error,
)
from beepboop.providers.llm.protocol import (
    AuthError,
    Caps,
    Event,
    Message,
    Request,
    RequestMetadata,
    SystemBlock,
    TextPart,
)

if TYPE_CHECKING:
    from mypy_boto3_bedrock_runtime.client import BedrockRuntimeClient
    from mypy_boto3_bedrock_runtime.type_defs import (
        ConverseRequestTypeDef,
        ConverseResponseTypeDef,
        ConverseStreamOutputTypeDef,
        ConverseStreamRequestTypeDef,
        ConverseStreamResponseTypeDef,
    )

log = get_logger(__name__)

#: Idle time after which ``warm()`` re-primes the pooled connection (§3.7).
WARM_IDLE_S = 60.0
#: Upper bound for one ``warm()`` call; it is never on the critical path.
WARM_TIMEOUT_S = 0.8


class ConverseClient(Protocol):
    """The two ``bedrock-runtime`` client methods the provider uses.

    A real ``boto3.client("bedrock-runtime")`` satisfies it; tests pass fakes.
    """

    def converse_stream(
        self, **kwargs: Unpack[ConverseStreamRequestTypeDef]
    ) -> ConverseStreamResponseTypeDef: ...

    def converse(self, **kwargs: Unpack[ConverseRequestTypeDef]) -> ConverseResponseTypeDef: ...


class _ClosableStream(Protocol):
    def __iter__(self) -> Iterator[object]: ...
    def close(self) -> None: ...


# ---- settings ---------------------------------------------------------------


class BedrockSettings(BaseModel):
    """Household Bedrock settings (§2.1). Defaults come from ``manifest.yaml``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    region: str
    model_fast: str
    model_smart: str
    credentials: Literal["access_keys", "bearer_token", "ambient"]
    guardrail_id: str | None = None
    guardrail_version: str | None = None

    @classmethod
    def from_manifest(cls, **overrides: object) -> Self:
        """Settings with the manifest's defaults, overridden by ``overrides``."""
        manifest = load_manifest()
        properties = cast("dict[str, dict[str, object]]", manifest["settings"]["properties"])
        values: dict[str, object] = {
            key: spec["default"] for key, spec in properties.items() if "default" in spec
        }
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls.model_validate(values)


def load_manifest() -> dict[str, Any]:  # Any: an arbitrary YAML document
    """Parse ``bedrock/manifest.yaml`` shipped next to this module."""
    text = resources.files(__package__ or __name__).joinpath("manifest.yaml").read_text("utf-8")
    loaded: object = yaml.safe_load(text)
    if not isinstance(loaded, dict):
        msg = "bedrock/manifest.yaml is not a mapping"
        raise TypeError(msg)
    return cast("dict[str, Any]", loaded)


class _StaticTokenProvider:
    """botocore ``token_provider`` component that returns one fixed bearer token.

    Used for ``credentials: bearer_token`` (a long-term Bedrock API key, §3.5),
    so the key goes from the secret store straight to botocore's bearer signer
    and never through ``AWS_BEARER_TOKEN_BEDROCK`` in the process environment.
    """

    def __init__(self, token: SecretStr) -> None:
        self._token = token

    def load_token(self, **_kwargs: object) -> FrozenAuthToken:
        return FrozenAuthToken(self._token.get_secret_value())


def make_client(
    region: str,
    *,
    profile: str | None = None,
    access_key_id: SecretStr | None = None,
    secret_access_key: SecretStr | None = None,
    bearer_token: SecretStr | None = None,
    read_timeout_s: float = 10.0,
) -> ConverseClient:
    """A ``bedrock-runtime`` client configured for voice latency (§3.6, §3.7).

    No retries (the ladder fails sideways instead), a 1 s connect timeout,
    TCP keep-alive and a two-connection pool kept warm by ``warm()``.
    Credentials, in order: ``bearer_token`` (Bedrock API key, sent as
    ``Authorization: Bearer``), else access keys (SigV4), else ambient
    (``profile`` / env / instance metadata).
    """
    import boto3
    import botocore.session

    config = Config(
        region_name=region,
        # botocore: "max_attempts" counts retries (1 => 2 sends); "total_max_attempts"
        # counts sends. One send, no retry: the ladder fails sideways (§3.6).
        retries={"total_max_attempts": 1, "mode": "standard"},
        connect_timeout=1.0,
        read_timeout=read_timeout_s,
        max_pool_connections=2,
        tcp_keepalive=True,
    )
    if bearer_token is not None:
        # Pin the bearer signer in code: botocore otherwise picks bearer only when
        # AWS_BEARER_TOKEN_BEDROCK is set, and we never put the key in the env.
        config = config.merge(Config(signature_version="bearer"))
        core_session = botocore.session.Session()
        core_session.register_component("token_provider", _StaticTokenProvider(bearer_token))
        session = boto3.Session(botocore_session=core_session, region_name=region)
    elif access_key_id is not None and secret_access_key is not None:
        session = boto3.Session(
            aws_access_key_id=access_key_id.get_secret_value(),
            aws_secret_access_key=secret_access_key.get_secret_value(),
            region_name=region,
        )
    else:
        session = boto3.Session(profile_name=profile, region_name=region)
    client: BedrockRuntimeClient = session.client(  # pyright: ignore[reportUnknownMemberType] - boto3-stubs overload set is partially unknown
        "bedrock-runtime", config=config
    )
    return client


# ---- provider ---------------------------------------------------------------

_END = object()


class BedrockProvider:
    """``LLMProvider`` for Bedrock ConverseStream (§3)."""

    id = "bedrock"
    license = "Apache-2.0"
    caps = Caps(
        tools=True,
        parallel_tools=True,
        streaming_tool_args=True,
        cache=True,
        effort=True,
        max_ctx=200_000,  # conservative; the 5.5 models accept more
        max_output=32_000,  # conservative; voice turns use max_tokens=400
        guardrails=False,  # v0.2 (§11)
    )

    def __init__(
        self,
        client: ConverseClient,
        model_id: str,
        *,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._client = client
        self.model_id = model_id
        self._clock = clock
        self._last_used: float | None = None

    @classmethod
    def from_settings(
        cls,
        settings: BedrockSettings,
        *,
        alias: Literal["fast", "smart"] = "fast",
        profile: str | None = None,
        access_key_id: SecretStr | None = None,
        secret_access_key: SecretStr | None = None,
        bearer_token: SecretStr | None = None,
    ) -> Self:
        """Build a provider from household settings and the matching secrets.

        ``access_keys`` needs ``aws_access_key_id`` + ``aws_secret_access_key``;
        ``bearer_token`` needs ``bedrock_bearer_token``; ``ambient`` needs neither.
        Only the secrets of the selected mode are used.
        """
        if settings.credentials == "access_keys" and (
            access_key_id is None or secret_access_key is None
        ):
            msg = "credentials=access_keys needs aws_access_key_id and aws_secret_access_key"
            raise AuthError(msg, code="MissingKeys")
        if settings.credentials == "bearer_token" and not (
            bearer_token and bearer_token.get_secret_value()
        ):
            msg = "credentials=bearer_token needs bedrock_bearer_token"
            raise AuthError(msg, code="MissingKeys")
        if settings.credentials == "bearer_token":
            client = make_client(settings.region, bearer_token=bearer_token)
        elif settings.credentials == "access_keys":
            client = make_client(
                settings.region,
                access_key_id=access_key_id,
                secret_access_key=secret_access_key,
            )
        else:
            client = make_client(settings.region, profile=profile)
        model = settings.model_fast if alias == "fast" else settings.model_smart
        return cls(client, model)

    async def stream(self, req: Request) -> AsyncIterator[Event]:
        """Stream one response; see the protocol module for the contract."""
        params = build_converse_params(req, self.model_id)
        loop = asyncio.get_running_loop()
        translator = StreamTranslator()
        log.debug(
            "llm.request", provider=self.id, model=self.model_id, turn_id=req.metadata.turn_id
        )
        t_sent = self._clock()
        self._last_used = t_sent

        call = loop.run_in_executor(None, lambda: self._client.converse_stream(**params))
        try:
            response = await asyncio.shield(call)
        except asyncio.CancelledError:
            call.add_done_callback(_close_late_response)
            for event in translator.cancelled():
                yield event
            raise
        except Exception as exc:
            raise map_client_error(exc) from exc

        event_stream = cast("_ClosableStream", response["stream"])
        queue: asyncio.Queue[object] = asyncio.Queue()
        closed = threading.Event()
        threading.Thread(
            target=_pump,
            args=(event_stream, loop, queue, closed, self._clock),
            name="bedrock-stream",
            daemon=True,
        ).start()

        try:
            while True:
                item = await queue.get()
                if item is _END:
                    break
                if isinstance(item, BaseException):
                    raise map_client_error(item) from item
                raw, received = cast("tuple[ConverseStreamOutputTypeDef, float]", item)
                elapsed_ms = round((received - t_sent) * 1000)
                for event in translator.feed(raw, elapsed_ms):
                    yield event
            for event in translator.finish():
                yield event
        except asyncio.CancelledError:
            _close(event_stream, closed)
            for event in translator.cancelled():
                yield event
            raise
        finally:
            _close(event_stream, closed)
            self._last_used = self._clock()

    async def warm(self) -> None:
        """Re-open the pooled TLS connection if idle > 60 s; no tokens (§3.7)."""
        now = self._clock()
        if self._last_used is not None and now - self._last_used < WARM_IDLE_S:
            return
        try:
            await asyncio.wait_for(asyncio.to_thread(self._prime_connection), WARM_TIMEOUT_S)
        except (TimeoutError, OSError, BotoCoreError, ClientError) as exc:
            log.warning("llm.warm_failed", provider=self.id, error=type(exc).__name__)
            return
        self._last_used = self._clock()

    def _prime_connection(self) -> None:
        """Replace pooled connections with one fresh TCP+TLS connection.

        Idle keep-alive connections can be silently dropped by home NAT
        routers; reusing one then hangs until the read timeout (measured on
        the 2026-11-02 workstation probe after a 6-minute gap). So the pool
        is cleared first, then one unsigned ``GET /`` (answered 404 without
        invoking a model, no tokens) opens a new connection that stays in the
        pool for the real request. Fakes without a botocore endpoint: no-op.
        """
        endpoint: object = getattr(self._client, "_endpoint", None)
        meta: object = getattr(self._client, "meta", None)
        session: object = getattr(endpoint, "http_session", None)
        url: object = getattr(meta, "endpoint_url", None)
        send: object = getattr(session, "send", None)
        close: object = getattr(session, "close", None)
        if not callable(send) or not isinstance(url, str):
            return
        if callable(close):
            close()
        send(AWSRequest(method="GET", url=url + "/").prepare())

    async def healthcheck(self) -> str:
        """``"ok"`` or a short reason. One 1-token ``Converse`` call (§3.5)."""
        req = Request(
            system=[SystemBlock(text="Reply with one word.")],
            messages=[Message(role="user", content=[TextPart(text="ping")])],
            max_tokens=1,
            metadata=RequestMetadata(profile_id="system", turn_id="healthcheck", kind="warm"),
        )
        params = build_converse_params(req, self.model_id)
        try:
            await asyncio.to_thread(
                lambda: self._client.converse(
                    modelId=params["modelId"],
                    system=params.get("system", []),
                    messages=params.get("messages", []),
                    inferenceConfig={"maxTokens": 1},
                )
            )
        except Exception as exc:
            error = map_client_error(exc)
            if error.code == "AccessDeniedException":
                return f"no model access for {self.model_id} (request it in the Bedrock console)"
            if isinstance(error, AuthError):
                return f"credentials rejected ({error.code})"
            return f"{type(error).__name__}: {error.code or error}"
        return "ok"


def _pump(
    event_stream: _ClosableStream,
    loop: asyncio.AbstractEventLoop,
    queue: asyncio.Queue[object],
    closed: threading.Event,
    clock: Callable[[], float],
) -> None:
    """Reader thread: push ``(raw event, receive time)`` or the error to the loop.

    The receive time is taken here, on the reader thread, so TTFT does not
    include event-loop scheduling delay.
    """

    def put(item: object) -> None:
        with contextlib.suppress(RuntimeError):  # loop already closed
            loop.call_soon_threadsafe(queue.put_nowait, item)

    try:
        for raw in event_stream:
            if closed.is_set():
                return
            put((raw, clock()))
    except Exception as exc:
        if not closed.is_set():  # a read error after close() is expected
            put(exc)
            return
    put(_END)


def _close(event_stream: _ClosableStream, closed: threading.Event) -> None:
    """Close the HTTP stream once; the reader thread then unblocks and exits."""
    if closed.is_set():
        return
    closed.set()
    try:
        event_stream.close()
    except Exception as exc:  # closing a half-read socket can raise; never mask the caller
        log.debug("llm.stream_close_error", error=type(exc).__name__)


def _close_late_response(future: asyncio.Future[ConverseStreamResponseTypeDef]) -> None:
    """Close a stream whose request completed after the caller was cancelled."""
    if future.cancelled() or future.exception() is not None:
        return
    _close(cast("_ClosableStream", future.result()["stream"]), threading.Event())
