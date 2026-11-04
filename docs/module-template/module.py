"""parcel — worked example module (docs/module-template).

Rules this file follows (docs/MODULES.md §2, §5):
  * never import mcp / homeassistant / boto3 / anthropic / openai / websockets / beepboop.ha
    / sqlite3 / socket
  * all I/O goes through ctx.http, ctx.kv, ctx.secrets; all logging through ctx.log
  * every handler is safe under ctx.dry_run (ctx.http records non-GET effects instead of sending)
  * handlers return a dict matching manifest `output`, or raise ToolError with an actionable message
  * the module answers when asked; there is no background polling in v0.1 (docs/MODULES.md §9)

The carrier API below is fictional. Replace `_CarrierClient` with your integration.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from beepboop.module import (
    Health,
    ToolContext,
    ToolError,
    tool,
)
from beepboop.module import (
    Module as BaseModule,
)

KV_PARCELS = (
    "parcels"  # per-profile key: list[dict]  (ctx.kv is scoped to (module, profile) in a tool call)
)


@dataclass(frozen=True)
class _Parcel:
    tracking_number: str
    carrier: str
    label: str
    status: str = "unknown"
    eta: str | None = None
    last_update: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tracking_number": self.tracking_number,
            "carrier": self.carrier,
            "label": self.label,
            "status": self.status,
            "eta": self.eta,
            "last_update": self.last_update,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> _Parcel:
        return cls(
            **{
                k: d.get(k)
                for k in ("tracking_number", "carrier", "label", "status", "eta", "last_update")
            }
        )


class _CarrierClient:
    """Thin wrapper over the (fictional) aggregator API.

    Uses only ctx.http; the host is in manifest `egress`.
    """

    BASE = "https://api.example-carrier.test/v1"

    def __init__(self, http, api_key: str) -> None:
        self._http = http
        self._headers = {"Authorization": f"Bearer {api_key}"}

    async def detect_carrier(self, tracking_number: str) -> str:
        r = await self._http.get(
            f"{self.BASE}/detect", params={"n": tracking_number}, headers=self._headers
        )
        self._raise_for_auth(r)
        return (r.json().get("carrier") or "unknown").lower()

    async def status(self, tracking_number: str, carrier: str) -> dict[str, Any]:
        r = await self._http.get(
            f"{self.BASE}/track/{carrier}/{tracking_number}", headers=self._headers
        )
        self._raise_for_auth(r)
        if r.status_code == 404:
            raise ToolError(
                "That tracking number is not known to the carrier yet. "
                "Suggest checking it again later.",
                code="not_found",
            )
        if r.status_code == 429:
            raise ToolError(
                "The carrier is rate limiting us. Tell the user to try again in a few minutes.",
                code="rate_limited",
                retryable=True,
            )
        if r.status_code >= 500:
            raise ToolError(
                "The carrier service is unavailable right now.", code="unavailable", retryable=True
            )
        return r.json()

    @staticmethod
    def _raise_for_auth(r) -> None:
        if r.status_code in (401, 403):
            # code="reauth" also flips this integration's health so the UI shows "reconnect"
            raise ToolError(
                "Carrier login failed. "
                "Tell the user to re-enter the carrier API key in the beepboop app.",
                code="reauth",
            )


class Module(BaseModule):
    """Entry point. The core instantiates exactly one `Module` per module folder."""

    # ---------------------------------------- lifecycle (hooks get a ToolContext with no caller)

    async def setup(self, ctx: ToolContext) -> None:
        # Nothing to warm: state lives in ctx.kv per profile and is read on demand.
        ctx.log.info("parcel.setup")

    async def health(self, ctx: ToolContext) -> Health:
        # Called per connected profile (ctx.profile_id set, ctx.secrets scoped to that person),
        # every 10 min and when the person presses "Test and save".
        key = await ctx.secrets.get("api_key")
        if not key:
            return Health(
                "error",
                "No carrier API key set.",
                spoken="Parcel tracking isn't set up yet. Add a carrier key in the beepboop app.",
            )
        try:
            await _CarrierClient(ctx.http, key).detect_carrier("1Z999AA10123456784")
        except ToolError as e:
            if e.code == "reauth":
                return Health(
                    "reauth",
                    "Carrier rejected the API key.",
                    spoken="Your parcel tracking needs reconnecting in the beepboop app.",
                )
            return Health("degraded", f"Carrier check failed: {e.code}")
        return Health("ok", "Signed in to the carrier aggregator.")

    async def teardown(self, ctx: ToolContext) -> None:
        ctx.log.info("parcel.teardown")

    # ------------------------------------------------------------------ tools

    @tool("parcel.track")
    async def track(
        self, ctx: ToolContext, tracking_number: str | None = None, query: str | None = None
    ) -> dict[str, Any]:
        parcels = await self._load(ctx)
        if tracking_number:
            parcels = [p for p in parcels if p.tracking_number == _norm(tracking_number)]
        if query:
            q = query.lower()
            parcels = [p for p in parcels if q in p.label.lower() or q in p.carrier]
        if not parcels:
            return {
                "speak": "You aren't tracking any parcels right now."
                if not (tracking_number or query)
                else "I couldn't find a parcel matching that.",
                "parcels": [],
            }

        client = await self._client(ctx)
        fresh: list[_Parcel] = []
        for p in parcels[:10]:
            try:
                data = await client.status(p.tracking_number, p.carrier)
                fresh.append(_merge(p, data))
            except ToolError as e:
                if e.code in ("rate_limited", "unavailable"):
                    fresh.append(p)  # fall back to the cached status
                else:
                    raise
        await self._save(ctx, _replace(await self._load(ctx), fresh))
        ctx.log.info("parcel.track", n=len(fresh))  # never log labels or numbers: personal data
        return {"speak": _speak_status(fresh, ctx.now()), "parcels": [p.to_dict() for p in fresh]}

    @tool("parcel.add")
    async def add(
        self,
        ctx: ToolContext,
        tracking_number: str,
        label: str | None = None,
        carrier: str = "auto",
    ) -> dict[str, Any]:
        number = _norm(tracking_number)
        if len(number) < 8 or len(number) > 40:
            raise ToolError(
                "That doesn't look like a tracking number. Ask the user to repeat it.",
                code="invalid_args",
            )
        parcels = await self._load(ctx)
        if any(p.tracking_number == number for p in parcels):
            p = next(p for p in parcels if p.tracking_number == number)
            return {
                "speak": f"You're already tracking {p.label}.",
                "tracking_number": number,
                "carrier": p.carrier,
                "already_tracked": True,
            }

        client = await self._client(ctx)
        if carrier == "auto":
            carrier = (
                ctx.settings["carrier"]
                if ctx.settings["carrier"] != "auto"
                else await client.detect_carrier(number)
            )
        p = _Parcel(number, carrier, label or carrier.upper())
        parcels.append(p)
        await self._save(ctx, parcels)
        return {
            "speak": f"Tracking {p.label} with {carrier.upper()}.",
            "tracking_number": number,
            "carrier": carrier,
            "already_tracked": False,
        }

    @tool("parcel.remove")
    async def remove(
        self, ctx: ToolContext, tracking_number: str | None = None, query: str | None = None
    ) -> dict[str, Any]:
        parcels = await self._load(ctx)
        if tracking_number:
            hits = [p for p in parcels if p.tracking_number == _norm(tracking_number)]
        elif query and query.lower().strip() == "delivered":
            hits = [p for p in parcels if p.status == "delivered"]
        elif query:
            q = query.lower()
            hits = [p for p in parcels if q in p.label.lower()]
        else:
            hits = []
        if not hits:
            return {
                "speak": "I couldn't find a parcel matching that.",
                "removed": [],
                "status": "none_matched",
            }
        if len(hits) > 1 and not (query and query.lower().strip() == "delivered"):
            names = ", ".join(p.label for p in hits[:5])
            return {"speak": f"Which one: {names}?", "removed": [], "status": "needs_clarification"}
        keep = [p for p in parcels if p not in hits]
        await self._save(ctx, keep)
        return {
            "speak": f"Stopped tracking {', '.join(p.label for p in hits)}.",
            "removed": [p.label for p in hits],
            "status": "ok",
        }

    # ------------------------------------------------------------------ helpers

    async def _client(self, ctx: ToolContext) -> _CarrierClient:
        key = await ctx.secrets.get("api_key")
        if not key:
            raise ToolError(
                "Parcel tracking isn't set up. "
                "Tell the user to add a carrier API key in the beepboop app.",
                code="reauth",
            )
        return _CarrierClient(ctx.http, key)

    async def _load(self, ctx: ToolContext) -> list[_Parcel]:
        return [_Parcel.from_dict(d) for d in await ctx.kv.get(KV_PARCELS, [])]

    async def _save(self, ctx: ToolContext, parcels: list[_Parcel]) -> None:
        await ctx.kv.set(KV_PARCELS, [p.to_dict() for p in parcels])


# -------------------------------------------- pure helpers (unit-tested in tests/test_module.py)


def _norm(n: str) -> str:
    return "".join(n.replace("-", " ").split()).upper()


def _merge(p: _Parcel, data: dict[str, Any]) -> _Parcel:
    return _Parcel(
        p.tracking_number,
        p.carrier,
        p.label,
        status=str(data.get("status") or "unknown"),
        eta=data.get("eta"),
        last_update=str(data.get("summary") or "")[:200],
    )


def _replace(all_: list[_Parcel], fresh: list[_Parcel]) -> list[_Parcel]:
    by_num = {p.tracking_number: p for p in fresh}
    return [by_num.get(p.tracking_number, p) for p in all_]


def _speak_status(parcels: list[_Parcel], now: datetime) -> str:
    """One sentence, no markdown, <= ~25 words.

    The model usually rephrases; the fallback reads this verbatim.
    """
    if len(parcels) == 1:
        p = parcels[0]
        return f"{p.label.capitalize()} is {_status_words(p, now)}."
    parts = [f"{p.label} {_status_words(p, now)}" for p in parcels[:3]]
    more = f", and {len(parcels) - 3} more" if len(parcels) > 3 else ""
    return "; ".join(parts) + more + "."


def _status_words(p: _Parcel, now: datetime) -> str:
    if p.status == "delivered":
        return "delivered"
    if p.status == "out_for_delivery":
        return "out for delivery today"
    if p.status == "exception":
        return "delayed by the carrier"
    if p.eta:
        try:
            d = datetime.fromisoformat(p.eta)
            days = (d.date() - now.date()).days
            when = "today" if days == 0 else "tomorrow" if days == 1 else d.strftime("%A")
            return f"arriving {when}"
        except ValueError:
            pass
    return "in transit" if p.status == "in_transit" else "not moving yet"
