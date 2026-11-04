"""BedrockProvider behaviour beyond the conformance suite: errors, warm, health."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest
from botocore.awsrequest import AWSPreparedRequest
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from pydantic import SecretStr

from beepboop.providers.llm import AuthError, Done, Throttled, Unavailable, Usage
from beepboop.providers.llm.bedrock import provider as provider_module
from beepboop.providers.llm.bedrock.fake import FakeConverseClient, RawEvent
from beepboop.providers.llm.bedrock.provider import (
    BedrockProvider,
    BedrockSettings,
    load_manifest,
    make_client,
)
from beepboop.providers.llm.conformance import collect, text_request

MODEL = "global.anthropic.claude-haiku-5-5"


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "ConverseStream")


@pytest.fixture
def real_client() -> Any:  # Any: botocore client, used with Stubber
    """A real botocore bedrock-runtime client with dummy keys (never sends)."""
    return make_client(
        "us-west-2",
        access_key_id=SecretStr("AKIDEXAMPLE"),
        secret_access_key=SecretStr("dummy"),
    )


# ---- settings ---------------------------------------------------------------


def test_settings_defaults_come_from_the_manifest() -> None:
    settings = BedrockSettings.from_manifest()
    assert settings.model_fast == MODEL
    assert settings.model_smart == "global.anthropic.claude-sonnet-5-5"
    assert settings.credentials == "access_keys"
    assert load_manifest()["id"] == "bedrock"


def test_settings_overrides() -> None:
    settings = BedrockSettings.from_manifest(region="eu-west-1", credentials="ambient")
    assert (settings.region, settings.credentials) == ("eu-west-1", "ambient")


def test_access_keys_mode_needs_keys() -> None:
    with pytest.raises(AuthError, match="aws_access_key_id"):
        BedrockProvider.from_settings(BedrockSettings.from_manifest())


def test_bearer_token_mode_needs_the_token() -> None:
    settings = BedrockSettings.from_manifest(credentials="bearer_token")
    with pytest.raises(AuthError, match="bedrock_bearer_token"):
        BedrockProvider.from_settings(settings)
    with pytest.raises(AuthError, match="bedrock_bearer_token"):
        BedrockProvider.from_settings(settings, bearer_token=SecretStr(""))


class _CapturedError(Exception):
    """Raised from ``before-send`` so the captured request never leaves the process."""

    def __init__(self, request: AWSPreparedRequest) -> None:
        super().__init__("captured")
        self.request = request


def _capture_converse(client: Any) -> AWSPreparedRequest:  # Any: botocore client
    def stop(request: AWSPreparedRequest, **_kwargs: object) -> None:
        raise _CapturedError(request)

    client.meta.events.register("before-send.bedrock-runtime.Converse", stop)
    with pytest.raises(_CapturedError) as caught:
        client.converse(
            modelId=MODEL,
            messages=[{"role": "user", "content": [{"text": "ping"}]}],
            inferenceConfig={"maxTokens": 1},
        )
    return caught.value.request


def _header(request: AWSPreparedRequest, name: str) -> str:
    value: object = request.headers[name]
    return value.decode() if isinstance(value, bytes) else str(value)


@pytest.fixture
def no_ambient_aws(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:  # Any: Path
    """No AWS env credentials, config files or bearer env var: only what we pass in."""
    import os

    for name in list(os.environ):
        if name.startswith("AWS_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


@pytest.mark.usefixtures("no_ambient_aws")
def test_bearer_token_is_sent_as_authorization_bearer() -> None:
    """``credentials: bearer_token`` signs with the Bedrock API key, not SigV4 (§3.5)."""
    import os

    settings = BedrockSettings.from_manifest(region="us-west-2", credentials="bearer_token")
    token = SecretStr("ABSKexample-bedrock-api-key")
    provider = BedrockProvider.from_settings(settings, bearer_token=token)

    request = _capture_converse(provider._client)  # pyright: ignore[reportPrivateUsage] - test inspects the wire

    assert _header(request, "Authorization") == f"Bearer {token.get_secret_value()}"
    assert "X-Amz-Date" not in request.headers  # no SigV4
    assert request.url.startswith("https://bedrock-runtime.us-west-2.amazonaws.com/")
    assert "AWS_BEARER_TOKEN_BEDROCK" not in os.environ  # never via the environment


@pytest.mark.usefixtures("no_ambient_aws")
def test_access_keys_are_sent_as_sigv4() -> None:
    settings = BedrockSettings.from_manifest(region="us-west-2")
    provider = BedrockProvider.from_settings(
        settings, access_key_id=SecretStr("AKIDEXAMPLE"), secret_access_key=SecretStr("x")
    )

    request = _capture_converse(provider._client)  # pyright: ignore[reportPrivateUsage] - test inspects the wire

    assert _header(request, "Authorization").startswith("AWS4-HMAC-SHA256 Credential=AKIDEX")


def test_from_settings_picks_the_alias_model() -> None:
    settings = BedrockSettings.from_manifest(region="us-west-2")
    key_id, secret = SecretStr("AKIDEXAMPLE"), SecretStr("x")
    fast = BedrockProvider.from_settings(settings, access_key_id=key_id, secret_access_key=secret)
    assert fast.model_id == MODEL
    smart = BedrockProvider.from_settings(
        settings, alias="smart", access_key_id=key_id, secret_access_key=secret
    )
    assert smart.model_id == settings.model_smart


def test_client_does_not_retry_and_has_voice_timeouts(real_client: Any) -> None:  # Any: client
    config = real_client.meta.config
    assert config.retries["total_max_attempts"] == 1  # one send, no retry
    assert config.connect_timeout == 1.0
    assert config.max_pool_connections == 2


# ---- errors -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "cls"),
    [("ThrottlingException", Throttled), ("AccessDeniedException", AuthError)],
)
async def test_request_errors_are_mapped(code: str, cls: type[Exception]) -> None:
    provider = BedrockProvider(FakeConverseClient([[]], error=_client_error(code)), MODEL)
    with pytest.raises(cls):
        await collect(provider, text_request())


async def test_stubbed_botocore_client_error_is_mapped(real_client: Any) -> None:  # Any: client
    with Stubber(real_client) as stub:
        stub.add_client_error("converse_stream", service_error_code="ServiceUnavailableException")
        provider = BedrockProvider(real_client, MODEL)
        with pytest.raises(Unavailable):
            await collect(provider, text_request())


async def test_mid_stream_error_is_raised(text_events: list[RawEvent]) -> None:
    broken = [*text_events[:3], {"modelStreamErrorException": {"message": "boom"}}]
    provider = BedrockProvider(FakeConverseClient([broken]), MODEL)
    with pytest.raises(Unavailable, match="boom"):
        await collect(provider, text_request())


async def test_stream_is_closed_after_a_normal_finish(text_events: list[RawEvent]) -> None:
    client = FakeConverseClient([text_events])
    events = await collect(BedrockProvider(client, MODEL), text_request())
    assert isinstance(events[-2], Usage)
    assert isinstance(events[-1], Done)
    assert client.last_closed_at() is not None


async def test_cancel_before_response_still_finalises() -> None:
    release = threading.Event()

    class SlowClient(FakeConverseClient):
        def converse_stream(self, **kwargs: Any) -> Any:  # Any: test double
            release.wait(timeout=5)
            return super().converse_stream(**kwargs)

    client = SlowClient([[]])
    events: list[object] = []

    async def consume() -> None:
        async for event in BedrockProvider(client, MODEL).stream(text_request()):
            events.append(event)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)  # the request is now blocked in the executor
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [type(e) for e in events] == [Usage, Done]
    release.set()
    for _ in range(50):  # the late response is closed by a done-callback
        if client.last_closed_at() is not None:
            break
        await asyncio.sleep(0.01)
    assert client.last_closed_at() is not None


# ---- warm and health --------------------------------------------------------


async def test_warm_primes_through_the_client_pool() -> None:
    sent: list[AWSPreparedRequest] = []
    closes: list[int] = []

    class Session:
        def send(self, request: AWSPreparedRequest) -> None:
            sent.append(request)

        def close(self) -> None:
            closes.append(len(sent))

    class Endpoint:
        http_session = Session()

    class Meta:
        endpoint_url = "https://bedrock-runtime.us-west-2.amazonaws.com"

    client = FakeConverseClient([[]])
    client._endpoint = Endpoint()  # type: ignore[attr-defined]
    client.meta = Meta()  # type: ignore[attr-defined]
    now = [0.0]
    provider = BedrockProvider(client, MODEL, clock=lambda: now[0])

    await provider.warm()
    assert closes == [0]  # stale pooled connections are dropped before priming
    assert [r.method for r in sent] == ["GET"]
    assert sent[0].url == "https://bedrock-runtime.us-west-2.amazonaws.com/"

    now[0] = 30.0  # hot pool: nothing to do
    await provider.warm()
    assert len(sent) == 1

    now[0] = 100.0  # idle > 60 s: prime again
    await provider.warm()
    assert len(sent) == 2


async def test_warm_failure_is_logged_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = BedrockProvider(FakeConverseClient([[]]), MODEL)

    def fail() -> None:
        raise OSError("network down")

    monkeypatch.setattr(provider, "_prime_connection", fail)
    await provider.warm()  # does not raise


async def test_healthcheck_ok_sends_one_token(real_client: Any) -> None:  # Any: client
    with Stubber(real_client) as stub:
        stub.add_response(
            "converse",
            {
                "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
                "stopReason": "max_tokens",
                "usage": {"inputTokens": 9, "outputTokens": 1, "totalTokens": 10},
                "metrics": {"latencyMs": 1},
            },
            {
                "modelId": MODEL,
                "system": [{"text": "Reply with one word."}],
                "messages": [{"role": "user", "content": [{"text": "ping"}]}],
                "inferenceConfig": {"maxTokens": 1},
            },
        )
        assert await BedrockProvider(real_client, MODEL).healthcheck() == "ok"


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("AccessDeniedException", "no model access"),
        ("UnrecognizedClientException", "credentials rejected"),
        ("ThrottlingException", "Throttled"),
    ],
)
async def test_healthcheck_reasons(code: str, expected: str) -> None:
    provider = BedrockProvider(FakeConverseClient([[]], error=_client_error(code)), MODEL)
    assert expected in await provider.healthcheck()


def test_module_constants() -> None:
    assert provider_module.WARM_IDLE_S == 60.0
    assert provider_module.WARM_TIMEOUT_S == 0.8
