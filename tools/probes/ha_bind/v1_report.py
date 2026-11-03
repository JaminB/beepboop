"""M0 item V1: does Home Assistant stay off the LAN, and which layer does the work?

ROADMAP V1: "The pinned HA version honours `http: server_host: 127.0.0.1` in a
container, and the nftables default-deny input chain drops LAN -> :8123. Record
which (or both) is load-bearing." HOME_ASSISTANT.md §2.1 describes the two layers.

Standard library only (Python >= 3.11) so it runs on a bare Pi OS host and on any
second LAN machine, outside the uv workspace. Two subcommands:

``lan``  run on a SECOND machine on the LAN. Connects to <pi>:8123 and to a
         control port (443) and prints one JSON object: whether :8123 timed out
         (dropped), was refused (RST) or connected.
``pi``   run on the Pi by verify_v1.sh, which collects the raw facts (``ss``,
         ``nft -j``, ``docker inspect``). Records one scenario into
         tools/bench/results/<date>-<host>-v1.json and recomputes the verdict.

A connection from the Pi to its own LAN address enters through ``lo`` and so
never exercises the input-chain drop; that is why the LAN side needs a second host.

Scenarios (the owner switches layers between runs; see README.md):

``both``       server_host set and nftables loaded: the shipped configuration.
``bind-only``  nftables table removed: is server_host alone enough?
``nft-only``   server_host removed (HA listens on all addresses): is nftables alone enough?

Nothing here invents a result: a field that was not observed is ``null`` and the
overall status stays ``INCOMPLETE`` until all three scenarios are recorded.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

HA_PORT = 8123
SCHEMA = 1
SCENARIOS = ("both", "bind-only", "nft-only")

Scenario = Literal["both", "bind-only", "nft-only"]
LanOutcome = Literal["timeout", "refused", "connected", "error"]
Status = Literal["PASS", "FAIL", "INCOMPLETE"]

JSONObj = dict[str, object]


# ---------------------------------------------------------------- LAN side ---


def tcp_probe(host: str, port: int, timeout_s: float) -> LanOutcome:
    """Classify one TCP connect: a silent drop times out, a closed port is refused."""
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return "connected"
    except TimeoutError:
        return "timeout"
    except ConnectionRefusedError:
        return "refused"
    except OSError:
        return "error"


def lan_probe(host: str, control_port: int = 22, timeout_s: float = 5.0) -> JSONObj:
    """The LAN-side measurement. The control port (SSH, allowed by the firewall and
    always listening during M0) proves the Pi answers at all, so a :8123 timeout
    means "dropped" rather than "wrong address"."""
    return {
        "probe": "V1-lan",
        "target": host,
        "port": HA_PORT,
        "outcome": tcp_probe(host, HA_PORT, timeout_s),
        "control_port": control_port,
        "control_outcome": tcp_probe(host, control_port, timeout_s),
        "timeout_s": timeout_s,
        "from_host": socket.gethostname(),
        "measured_at": _now(),
    }


# ----------------------------------------------------------------- Pi side ---


def parse_ss_listeners(ss_output: str, port: int = HA_PORT) -> list[str]:
    """Local addresses listening on ``port`` from ``ss -Hltn`` output.

    Lines look like ``LISTEN 0 128 127.0.0.1:8123 0.0.0.0:*`` or
    ``LISTEN 0 128 [::]:8123 [::]:*`` (IPv6 in brackets, sometimes ``*:8123``).
    """
    found: list[str] = []
    for line in ss_output.splitlines():
        cols = line.split()
        if len(cols) < 4 or cols[0] != "LISTEN":
            continue
        addr, _, port_s = cols[3].rpartition(":")
        if port_s == str(port):
            found.append(addr.strip("[]").split("%", 1)[0])
    return found


def is_loopback(addr: str) -> bool:
    if addr == "*":
        return False
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class NftSummary:
    """What the beepboop table's input chain does to :8123 (from ``nft -j list ruleset``)."""

    table_present: bool
    input_policy: str | None
    drops_8123: bool
    accepts_8123: bool


def _dport_matches(expr: object, port: int) -> bool:
    """True if one nft JSON expression is ``tcp dport == port`` (literal or in a literal set)."""
    if not isinstance(expr, dict):
        return False
    match = cast(JSONObj, expr).get("match")
    if not isinstance(match, dict):
        return False
    m = cast(JSONObj, match)
    left = m.get("left")
    if not isinstance(left, dict):
        return False
    payload = cast(JSONObj, left).get("payload")
    if not isinstance(payload, dict):
        return False
    p = cast(JSONObj, payload)
    if p.get("protocol") != "tcp" or p.get("field") != "dport":
        return False
    right = m.get("right")
    if isinstance(right, int):
        return right == port
    if isinstance(right, dict):
        members = cast(JSONObj, right).get("set")
        if isinstance(members, list):
            return port in cast(list[object], members)
    return False


def summarize_nft(ruleset: JSONObj, table: str = "beepboop", port: int = HA_PORT) -> NftSummary:
    items = ruleset.get("nftables")
    objects = cast(list[object], items) if isinstance(items, list) else []
    present = False
    policy: str | None = None
    drops = accepts = False
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        o = cast(JSONObj, obj)
        tbl, chain, rule = o.get("table"), o.get("chain"), o.get("rule")
        if isinstance(tbl, dict) and cast(JSONObj, tbl).get("name") == table:
            present = True
        if isinstance(chain, dict):
            c = cast(JSONObj, chain)
            if c.get("table") == table and c.get("hook") == "input":
                pol = c.get("policy")
                policy = pol if isinstance(pol, str) else None
        if isinstance(rule, dict):
            r = cast(JSONObj, rule)
            exprs = r.get("expr")
            if r.get("table") != table or not isinstance(exprs, list):
                continue
            ex = cast(list[object], exprs)
            if any(_dport_matches(e, port) for e in ex):
                drops = drops or any(isinstance(e, dict) and "drop" in e for e in ex)
                accepts = accepts or any(isinstance(e, dict) and "accept" in e for e in ex)
    return NftSummary(present, policy, drops, accepts)


def scenario_record(
    scenario: Scenario,
    listeners: list[str],
    nft: NftSummary | None,
    lan: JSONObj | None,
) -> JSONObj:
    """One scenario's observations plus what they say about each layer."""
    loopback_only = bool(listeners) and all(is_loopback(a) for a in listeners)
    reachable: bool | None = None
    blocked: bool | None = None
    if lan is not None:
        # connected or refused: either way the Pi answered on the control port.
        reachable = lan.get("control_outcome") in ("connected", "refused")
        # A drop only means something if the Pi was otherwise reachable from that host.
        if reachable:
            blocked = lan.get("outcome") in ("timeout", "refused")
    return {
        "scenario": scenario,
        "recorded_at": _now(),
        "listeners_8123": listeners,
        "listeners_loopback_only": loopback_only,
        "nft": asdict(nft) if nft else None,
        "lan": lan,
        "ha_listening": bool(listeners),
        "lan_reachable_control": reachable,
        "lan_blocked": blocked,
    }


def verdict(scenarios: dict[str, JSONObj]) -> JSONObj:
    """Combine the recorded scenarios into the V1 answer."""
    both, bind_only, nft_only = (scenarios.get(s) for s in SCENARIOS)

    # A run where HA was not listening at all proves nothing about either layer.
    layer1: bool | None = None  # server_host alone keeps HA off the LAN
    if (
        bind_only is not None
        and bind_only.get("ha_listening")
        and bind_only.get("lan_blocked") is not None
    ):
        layer1 = bool(bind_only["listeners_loopback_only"]) and bool(bind_only["lan_blocked"])

    layer2: bool | None = None  # nftables alone keeps HA off the LAN
    # Only valid if layer 1 really was off, i.e. HA listened on a non-loopback address.
    if (
        nft_only is not None
        and nft_only.get("ha_listening")
        and not nft_only.get("listeners_loopback_only")
        and nft_only.get("lan_reachable_control")
    ):
        lan = cast(JSONObj, nft_only["lan"])
        layer2 = lan.get("outcome") == "timeout"

    shipped_ok: bool | None = None
    if both is not None and both.get("ha_listening") and both.get("lan_blocked") is not None:
        # ROADMAP V1 asks which layer is load-bearing, so one layer failing alone is a
        # finding, not a failure; the shipped configuration letting the LAN in is.
        shipped_ok = bool(both["lan_blocked"])

    load_bearing: str | None = None
    if layer1 is not None and layer2 is not None:
        load_bearing = {
            (True, True): "both",
            (True, False): "server_host",
            (False, True): "nftables",
            (False, False): "neither",
        }[(layer1, layer2)]

    status: Status
    if shipped_ok is False or load_bearing == "neither":
        status = "FAIL"
    elif shipped_ok and load_bearing is not None:
        status = "PASS"
    else:
        status = "INCOMPLETE"
    return {
        "status": status,
        "shipped_config_blocks_lan": shipped_ok,
        "server_host_honoured": layer1,
        "nftables_drops_lan": layer2,
        "load_bearing": load_bearing,
    }


def merge_result(existing: JSONObj | None, header: JSONObj, record: JSONObj) -> JSONObj:
    """Add or replace one scenario in the result document and recompute the verdict."""
    doc: JSONObj = dict(existing) if existing else {}
    if doc.get("probe") not in (None, "V1"):
        raise ValueError("existing result file is not a V1 result")
    old = doc.get("scenarios")
    scenarios: dict[str, JSONObj] = (
        dict(cast(dict[str, JSONObj], old)) if isinstance(old, dict) else {}
    )
    scenarios[str(record["scenario"])] = record
    doc.update(header)
    doc.update(probe="V1", schema=SCHEMA, scenarios=scenarios, verdict=verdict(scenarios))
    return doc


# ---------------------------------------------------------------------- CLI ---


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _read_json(path: Path) -> JSONObj:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return cast(JSONObj, data)


def _cmd_lan(args: argparse.Namespace) -> int:
    result = lan_probe(
        cast(str, args.host), cast(int, args.control_port), cast(float, args.timeout)
    )
    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    return 0


def _cmd_pi(args: argparse.Namespace) -> int:
    ss_text = Path(cast(str, args.ss_file)).read_text(encoding="utf-8")
    nft_file = cast(str | None, args.nft_file)
    nft = summarize_nft(_read_json(Path(nft_file))) if nft_file else None
    lan_file = cast(str | None, args.lan_result)
    lan = _read_json(Path(lan_file)) if lan_file else None
    record = scenario_record(cast(Scenario, args.scenario), parse_ss_listeners(ss_text), nft, lan)
    host = cast(str, args.host)
    date = time.strftime("%Y-%m-%d")
    out_dir = Path(cast(str, args.results_dir))
    out_dir.mkdir(parents=True, exist_ok=True)
    out = Path(cast(str, args.out)) if args.out else out_dir / f"{date}-{host}-v1.json"
    header: JSONObj = {
        "host": host,
        "date": date,
        "ha": {
            "version": args.ha_version or None,
            "image": args.ha_image or None,
            "image_id": args.ha_image_id or None,
        },
        "pi_model": args.pi_model or None,
        "note": "measured on the Pi by tools/probes/ha_bind/verify_v1.sh",
    }
    doc = merge_result(_read_json(out) if out.exists() else None, header, record)
    out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    v = cast(JSONObj, doc["verdict"])
    sys.stdout.write(f"{out}\n{json.dumps(v, indent=2)}\n")
    return 1 if v["status"] == "FAIL" else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="v1_report.py", description=(__doc__ or "").split("\n\n")[0]
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    lan = sub.add_parser("lan", help="run on a second LAN host: probe <pi>:8123")
    lan.add_argument("host", help="the Pi's LAN address")
    lan.add_argument("--control-port", type=int, default=22)
    lan.add_argument("--timeout", type=float, default=5.0)
    lan.set_defaults(func=_cmd_lan)

    pi = sub.add_parser("pi", help="run on the Pi (via verify_v1.sh): record one scenario")
    pi.add_argument("--scenario", choices=SCENARIOS, required=True)
    pi.add_argument("--ss-file", required=True, help="output of `ss -Hltn`")
    pi.add_argument("--nft-file", help="output of `nft -j list ruleset`")
    pi.add_argument("--lan-result", help="JSON printed by the `lan` subcommand")
    pi.add_argument("--host", default=socket.gethostname())
    pi.add_argument("--ha-version", default="")
    pi.add_argument("--ha-image", default="")
    pi.add_argument("--ha-image-id", default="")
    pi.add_argument("--pi-model", default="")
    pi.add_argument("--results-dir", default="tools/bench/results")
    pi.add_argument(
        "--out", help="explicit output file (default <results-dir>/<date>-<host>-v1.json)"
    )
    pi.set_defaults(func=_cmd_pi)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    func = cast(Callable[[argparse.Namespace], int], args.func)
    return func(args)


if __name__ == "__main__":
    raise SystemExit(main())
