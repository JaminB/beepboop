"""In-memory fakes behind a dry-run ``ToolContext`` (docs/MODULES.md §12.3).

Every side effect is appended to one shared ``effects`` list as a plain dict with a
``kind`` key (``kv``, ``http``, ``ha``, ``announce``, ``secret``), which is what evals
compare against ``expect.effects`` and what ``ctx.effects`` exposes in unit tests.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Literal

from beepboop.module.types import (
    SAFE_HTTP_METHODS,
    AnnouncePriority,
    EntityRef,
    EntityState,
    HttpMethod,
    Risk,
    ServiceResult,
    ToolError,
)
from beepboop.policy.errors import PolicyDenied
from beepboop.service_http import host_allowed

type Effect = dict[str, Any]


class FakeKV:
    """``KVStore`` over a dict. ``household()`` returns the ``(module, NULL)`` scope."""

    def __init__(
        self,
        data: dict[str, Any] | None,
        effects: list[Effect],
        *,
        household: FakeKV | None = None,
    ) -> None:
        self.data: dict[str, Any] = copy.deepcopy(data or {})
        self._effects = effects
        self._household = household

    async def get(self, key: str, default: Any = None) -> Any:
        return copy.deepcopy(self.data.get(key, default))

    async def set(self, key: str, value: Any) -> None:
        encoded = json.dumps(value)
        if len(encoded.encode()) > 64 * 1024:
            msg = "kv value exceeds 64 KiB"
            raise ValueError(msg)
        self.data[key] = json.loads(encoded)
        self._effects.append({"kind": "kv", "op": "set", "key": key})

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)
        self._effects.append({"kind": "kv", "op": "delete", "key": key})

    async def keys(self, prefix: str = "") -> list[str]:
        return sorted(k for k in self.data if k.startswith(prefix))

    def household(self) -> FakeKV:
        return self._household or self


class FakeSecrets:
    """``SecretStore`` seeded from ``fixtures.secrets``; ``set``/``delete`` record effects."""

    def __init__(self, values: Mapping[str, str] | None, effects: list[Effect]) -> None:
        self.values: dict[str, str] = dict(values or {})
        self._effects = effects

    async def get(self, name: str) -> str | None:
        return self.values.get(name)

    async def set(self, name: str, value: str) -> None:
        self.values[name] = value
        self._effects.append({"kind": "secret", "op": "set", "name": name})

    async def delete(self, name: str) -> None:
        self.values.pop(name, None)
        self._effects.append({"kind": "secret", "op": "delete", "name": name})


@dataclass(frozen=True, slots=True)
class HAStateFixture:
    """One ``fixtures.ha.states`` row (§12.1): all rows are granted to every default profile."""

    entity_id: str
    uuid: str
    state: str
    attributes: dict[str, Any] = field(default_factory=dict[str, Any])
    area_id: str | None = None
    device_class: str | None = None
    name: str | None = None

    def ref(self) -> EntityRef:
        domain = self.entity_id.split(".", 1)[0]
        name = self.name or str(self.attributes.get("friendly_name") or self.entity_id)
        return EntityRef(
            uuid=self.uuid,
            entity_id=self.entity_id,
            domain=domain,
            name=name,
            area_id=self.area_id,
            device_class=self.device_class,
        )

    def entity_state(self) -> EntityState:
        return EntityState(self.ref(), self.state, dict(self.attributes))


class FakeHA:
    """``HAClient`` over the fixture state table; ``call_service`` records an effect."""

    def __init__(
        self, states: Sequence[HAStateFixture], effects: list[Effect], *, in_tool_call: bool
    ) -> None:
        self.states_table = list(states)
        self._effects = effects
        self._in_tool_call = in_tool_call

    def _find(self, key: str) -> HAStateFixture | None:
        for s in self.states_table:
            if key in (s.uuid, s.entity_id):
                return s
        return None

    async def resolve(
        self, target: str, *, domains: list[str] | None = None, area: str | None = None
    ) -> list[EntityRef]:
        needle = target.lower().strip()
        out: list[EntityRef] = []
        for s in self.states_table:
            ref = s.ref()
            if domains and ref.domain not in domains:
                continue
            if area and ref.area_id != area:
                continue
            hay = {ref.entity_id.lower(), ref.name.lower(), ref.uuid.lower()}
            if needle in hay or any(needle in h for h in hay):
                out.append(ref)
        return out

    async def state(self, entity: EntityRef | str) -> EntityState | None:
        key = entity.uuid if isinstance(entity, EntityRef) else entity
        found = self._find(key)
        return None if found is None else found.entity_state()

    async def states(self, domains: list[str] | None = None) -> list[EntityState]:
        return [
            s.entity_state()
            for s in self.states_table
            if not domains or s.entity_id.split(".", 1)[0] in domains
        ]

    async def call_service(
        self,
        domain: str,
        service: str,
        targets: list[EntityRef],
        data: dict[str, Any] | None = None,
    ) -> ServiceResult:
        if not self._in_tool_call:
            raise PolicyDenied("no_grant", "act", tool=f"{domain}.{service}")
        self._effects.append(
            {
                "kind": "ha",
                "domain": domain,
                "service": service,
                "targets": [t.uuid for t in targets],
                "data": dict(data or {}),
            }
        )
        return ServiceResult(ok=True)


@dataclass(frozen=True, slots=True)
class HttpFixture:
    """One ``fixtures.http`` row: ``match: {method, url}`` → ``response: {status,
    json|text|text_file}``.
    """

    method: str
    url_glob: str
    status: int = 200
    json_body: Any = None
    text: str | None = None
    text_file: str | None = None

    def matches(self, method: str, url: str) -> bool:
        return self.method.upper() == method.upper() and fnmatchcase(url, self.url_glob)


class FakeResponse:
    def __init__(
        self, status_code: int, text: str, headers: Mapping[str, str] | None = None
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.headers: Mapping[str, str] = dict(headers or {})

    def json(self) -> Any:
        return json.loads(self.text) if self.text else None


class FakeHttp:
    """``HttpClient``: safe methods (GET, PROPFIND, REPORT) served from fixtures, other
    methods recorded (§12.3).

    Hosts not in ``allowed_hosts`` (the manifest's ``egress``) are refused before any
    lookup with the real client's rule (``host_allowed``), including ``setting:<name>``
    resolved against ``settings``.
    """

    def __init__(
        self,
        fixtures: Sequence[HttpFixture],
        effects: list[Effect],
        *,
        module_dir: Path | None = None,
        allowed_hosts: Sequence[str] | None = None,
        settings: Mapping[str, Any] | None = None,
    ) -> None:
        self.fixtures = list(fixtures)
        self._effects = effects
        self._module_dir = module_dir
        self._allowed = list(allowed_hosts) if allowed_hosts is not None else None
        self._settings = dict(settings or {})
        self.calls: list[tuple[str, str]] = []

    def _check_host(self, url: str) -> None:
        if self._allowed is None or host_allowed(url, self._allowed, self._settings):
            return
        host = url.split("://", 1)[-1].split("/", 1)[0].split("@")[-1].split(":")[0].lower()
        raise ToolError(f"host {host} is not in the manifest egress list", code="other")

    def _body(self, f: HttpFixture) -> str:
        if f.text_file is not None:
            base = self._module_dir or Path.cwd()
            return (base / f.text_file).read_text(encoding="utf-8")
        if f.text is not None:
            return f.text
        return json.dumps(f.json_body) if f.json_body is not None else ""

    async def request(
        self,
        method: HttpMethod,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        data: Any = None,
        timeout: float | None = None,
    ) -> FakeResponse:
        self._check_host(url)
        full = url
        if params:
            query = "&".join(f"{k}={v}" for k, v in params.items())
            full = f"{url}{'&' if '?' in url else '?'}{query}"
        self.calls.append((method, full))
        if method not in SAFE_HTTP_METHODS:
            self._effects.append({"kind": "http", "method": method, "url": full})
            return FakeResponse(200, "{}")
        for f in self.fixtures:
            if f.matches(method, full):
                return FakeResponse(f.status, self._body(f))
        raise ToolError(f"no http fixture for {method} {full}", code="unavailable")

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> FakeResponse:
        return await self.request("GET", url, params=params, headers=headers, timeout=timeout)

    async def post(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        data: Any = None,
        timeout: float | None = None,
    ) -> FakeResponse:
        return await self.request(
            "POST", url, params=params, headers=headers, json=json, data=data, timeout=timeout
        )

    async def put(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        data: Any = None,
        timeout: float | None = None,
    ) -> FakeResponse:
        return await self.request(
            "PUT", url, params=params, headers=headers, json=json, data=data, timeout=timeout
        )

    async def patch(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        data: Any = None,
        timeout: float | None = None,
    ) -> FakeResponse:
        return await self.request(
            "PATCH", url, params=params, headers=headers, json=json, data=data, timeout=timeout
        )

    async def delete(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> FakeResponse:
        return await self.request("DELETE", url, params=params, headers=headers, timeout=timeout)


class FakeAnnounce:
    def __init__(self, effects: list[Effect]) -> None:
        self._effects = effects

    async def __call__(
        self, text: str, *, profile_id: str | None = None, priority: AnnouncePriority = "normal"
    ) -> None:
        self._effects.append(
            {"kind": "announce", "text": text, "profile_id": profile_id, "priority": priority}
        )


class AllowAllGrants:
    """``GrantView`` that says yes to everything: policy already ran before a handler."""

    def allows(self, resource: str, risk: Risk) -> bool:
        return True

    def max_risk(self, resource: str) -> Risk | None:
        return "critical"

    def resources(self, kind: Literal["tool", "entity", "area", "config_entry"]) -> Sequence[str]:
        return ()


__all__ = [
    "AllowAllGrants",
    "Effect",
    "FakeAnnounce",
    "FakeHA",
    "FakeHttp",
    "FakeKV",
    "FakeResponse",
    "FakeSecrets",
    "HAStateFixture",
    "HttpFixture",
]
