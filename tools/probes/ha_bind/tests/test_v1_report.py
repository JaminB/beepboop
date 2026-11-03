"""Unit tests for the V1 probe logic. All inputs are hand-written fixtures in the
shape `ss`, `nft -j` and the LAN probe produce; no result here is a measurement."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import cast

import pytest
import v1_report as v1

SS_LOOPBACK = """\
LISTEN 0      4096         127.0.0.1:8123       0.0.0.0:*
LISTEN 0      128            0.0.0.0:22         0.0.0.0:*
LISTEN 0      128               [::]:22            [::]:*
"""
SS_ALL = """\
LISTEN 0      4096           0.0.0.0:8123       0.0.0.0:*
LISTEN 0      4096              [::]:8123          [::]:*
"""
SS_NONE = "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\n"


def _nft(policy: str = "drop", with_8123_rule: bool = True) -> v1.JSONObj:
    items: list[object] = [
        {"metainfo": {"version": "1.0.6", "json_schema_version": 1}},
        {"table": {"family": "inet", "name": "beepboop", "handle": 1}},
        {
            "chain": {
                "family": "inet",
                "table": "beepboop",
                "name": "input",
                "type": "filter",
                "hook": "input",
                "prio": 0,
                "policy": policy,
            }
        },
        {"table": {"family": "ip", "name": "nat", "handle": 2}},  # Docker's, ignored
    ]
    if with_8123_rule:
        items.append(
            {
                "rule": {
                    "family": "inet",
                    "table": "beepboop",
                    "chain": "input",
                    "expr": [
                        {
                            "match": {
                                "op": "==",
                                "left": {"payload": {"protocol": "tcp", "field": "dport"}},
                                "right": 8123,
                            }
                        },
                        {"drop": None},
                    ],
                }
            }
        )
    return {"nftables": items}


def _lan(outcome: str, control: str = "connected") -> v1.JSONObj:
    return {"probe": "V1-lan", "outcome": outcome, "control_outcome": control}


def _record(scenario: v1.Scenario, ss: str, lan: v1.JSONObj | None) -> v1.JSONObj:
    return v1.scenario_record(scenario, v1.parse_ss_listeners(ss), v1.summarize_nft(_nft()), lan)


# --------------------------------------------------------------- parsing ---


def test_parse_ss_listeners() -> None:
    assert v1.parse_ss_listeners(SS_LOOPBACK) == ["127.0.0.1"]
    assert v1.parse_ss_listeners(SS_ALL) == ["0.0.0.0", "::"]  # noqa: S104 - parsed text, no bind
    assert v1.parse_ss_listeners(SS_NONE) == []
    assert v1.parse_ss_listeners("LISTEN 0 1 [fe80::1%eth0]:8123 [::]:*\n") == ["fe80::1"]


@pytest.mark.parametrize(
    ("addr", "expected"),
    [("127.0.0.1", True), ("::1", True), ("0.0.0.0", False),  # noqa: S104 - data, no bind ("::", False), ("*", False),
     ("192.168.1.20", False), ("garbage", False)],
)  # fmt: skip
def test_is_loopback(addr: str, expected: bool) -> None:
    assert v1.is_loopback(addr) is expected


def test_summarize_nft_finds_drop_and_policy() -> None:
    s = v1.summarize_nft(_nft())
    assert s == v1.NftSummary(table_present=True, input_policy="drop", drops_8123=True,
                              accepts_8123=False)  # fmt: skip


def test_summarize_nft_port_in_set_and_missing_table() -> None:
    doc = _nft(with_8123_rule=False)
    rule = {
        "rule": {
            "table": "beepboop",
            "expr": [
                {
                    "match": {
                        "left": {"payload": {"protocol": "tcp", "field": "dport"}},
                        "right": {"set": [8080, 8123]},
                    }
                },
                {"accept": None},
            ],
        }
    }
    cast(list[object], doc["nftables"]).append(rule)
    s = v1.summarize_nft(doc)
    assert s.accepts_8123
    assert not s.drops_8123
    assert v1.summarize_nft({"nftables": []}) == v1.NftSummary(False, None, False, False)


# --------------------------------------------------------------- verdict ---


def _verdict(**records: v1.JSONObj) -> v1.JSONObj:
    return v1.verdict({k.replace("_", "-"): r for k, r in records.items()})


def test_both_layers_load_bearing_passes() -> None:
    v = _verdict(
        both=_record("both", SS_LOOPBACK, _lan("timeout")),
        bind_only=_record("bind-only", SS_LOOPBACK, _lan("refused")),
        nft_only=_record("nft-only", SS_ALL, _lan("timeout")),
    )
    assert v == {
        "status": "PASS",
        "shipped_config_blocks_lan": True,
        "server_host_honoured": True,
        "nftables_drops_lan": True,
        "load_bearing": "both",
    }


def test_server_host_ignored_means_nftables_is_load_bearing() -> None:
    v = _verdict(
        both=_record("both", SS_ALL, _lan("timeout")),
        bind_only=_record("bind-only", SS_ALL, _lan("connected")),
        nft_only=_record("nft-only", SS_ALL, _lan("timeout")),
    )
    assert v["server_host_honoured"] is False
    assert v["load_bearing"] == "nftables"
    # The shipped config still keeps the LAN out: a recorded finding, not a failure.
    assert v["shipped_config_blocks_lan"] is True
    assert v["status"] == "PASS"


def test_lan_connect_in_shipped_config_fails() -> None:
    v = _verdict(both=_record("both", SS_LOOPBACK, _lan("connected")))
    assert v["status"] == "FAIL"


def test_partial_runs_are_incomplete_not_guessed() -> None:
    v = _verdict(both=_record("both", SS_LOOPBACK, _lan("timeout")))
    assert v["status"] == "INCOMPLETE"
    assert v["load_bearing"] is None
    assert v["server_host_honoured"] is None


def test_unreachable_pi_proves_nothing() -> None:
    v = _verdict(both=_record("both", SS_LOOPBACK, _lan("timeout", control="timeout")))
    assert v["shipped_config_blocks_lan"] is None
    assert v["status"] == "INCOMPLETE"


def test_ha_not_running_proves_nothing() -> None:
    v = _verdict(
        both=_record("both", SS_LOOPBACK, _lan("timeout")),
        bind_only=_record("bind-only", SS_NONE, _lan("refused")),
        nft_only=_record("nft-only", SS_ALL, _lan("timeout")),
    )
    assert v["server_host_honoured"] is None
    assert v["status"] == "INCOMPLETE"


def test_nft_only_with_server_host_still_set_is_not_counted() -> None:
    v = _verdict(nft_only=_record("nft-only", SS_LOOPBACK, _lan("timeout")))
    assert v["nftables_drops_lan"] is None


def test_no_lan_result_is_incomplete() -> None:
    record = _record("both", SS_LOOPBACK, None)
    assert record["lan_blocked"] is None
    assert v1.verdict({"both": record})["status"] == "INCOMPLETE"


# ------------------------------------------------------------ merge / CLI ---


def test_merge_accumulates_scenarios() -> None:
    header: v1.JSONObj = {"host": "beepboop"}
    doc = v1.merge_result(None, header, _record("both", SS_LOOPBACK, _lan("timeout")))
    doc = v1.merge_result(doc, header, _record("bind-only", SS_LOOPBACK, _lan("refused")))
    assert set(cast(dict[str, object], doc["scenarios"])) == {"both", "bind-only"}
    assert doc["probe"] == "V1"
    with pytest.raises(ValueError, match="not a V1"):
        v1.merge_result({"probe": "G4"}, header, _record("both", SS_LOOPBACK, None))


def test_pi_cli_writes_result_file(tmp_path: Path) -> None:
    ss = tmp_path / "ss.txt"
    ss.write_text(SS_LOOPBACK, encoding="utf-8")
    nft = tmp_path / "nft.json"
    nft.write_text(json.dumps(_nft()), encoding="utf-8")
    lan = tmp_path / "lan.json"
    lan.write_text(json.dumps(_lan("timeout")), encoding="utf-8")
    out = tmp_path / "result.json"
    args = ["pi", "--scenario", "both", "--ss-file", str(ss), "--nft-file", str(nft),
            "--lan-result", str(lan), "--host", "testhost", "--out", str(out)]  # fmt: skip
    assert v1.main(args) == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["host"] == "testhost"
    assert doc["verdict"]["status"] == "INCOMPLETE"
    assert doc["scenarios"]["both"]["nft"]["drops_8123"] is True
    assert doc["ha"] == {"version": None, "image": None, "image_id": None}


# ------------------------------------------------------------- TCP probe ---


def test_tcp_probe_connected() -> None:
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        assert v1.tcp_probe("127.0.0.1", srv.getsockname()[1], 2.0) == "connected"


# Refused/timeout are faked: Windows retries SYNs to a closed loopback port, so a
# real closed port is not a portable way to get an RST.
@pytest.mark.parametrize(
    ("exc", "expected"),
    [(TimeoutError, "timeout"), (ConnectionRefusedError, "refused"), (OSError, "error")],
)
def test_tcp_probe_classifies_errors(
    monkeypatch: pytest.MonkeyPatch, exc: type[OSError], expected: str
) -> None:
    def fake_connect(*_args: object, **_kwargs: object) -> socket.socket:
        raise exc

    monkeypatch.setattr(v1.socket, "create_connection", fake_connect)
    assert v1.tcp_probe("192.0.2.1", 8123, 0.1) == expected
