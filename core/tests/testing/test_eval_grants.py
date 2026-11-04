"""``policy_state_for``: module tools are raised to their declared risk only where the
profile's preset (or a ``fixtures.profiles.<name>.grants`` override) names them (MODULES §12.1)."""

from __future__ import annotations

from datetime import UTC, datetime

from beepboop.policy import ALL_SCOPES, Principal, authorize
from beepboop.registry.manifest import parse_manifest
from beepboop.testing.evals import policy_state_for
from beepboop.testing.evals_model import parse_evals

MANIFEST = """
id: lists
api: 1
version: 0.1.0
summary: "Lists."
scope: household
category: productivity
tools:
  - name: list.add
    description: "Add items."
    risk: act
    untrusted_output: false
    args: {type: object, properties: {}, additionalProperties: false}
    output: {type: object, properties: {speak: {type: string}}}
"""

EVALS = """
api: 1
module: lists
fixtures:
  profiles:
    toddler:
      kind: child
      grants:
        - {resource: "tool_glob:home.*", max_risk: act}
"""

NOW = datetime(2026, 11, 1, 17, tzinfo=UTC)


def _decide(profile: str) -> str:
    manifest = parse_manifest(MANIFEST)
    state = policy_state_for(manifest, parse_evals(EVALS))
    tool = manifest.tool("list.add")
    assert tool is not None
    p = Principal(profile, "voice", 0.95, 0.30, "eval", ALL_SCOPES, False, "01T")
    return authorize(state, p, tool, {}, [], NOW).outcome


def test_presets_that_name_the_tool_allow_it() -> None:
    assert _decide("member") == "allow"
    assert _decide("child") == "allow"  # child preset has list.* at act


def test_guest_preset_without_the_tool_is_denied_as_in_production() -> None:
    assert _decide("guest") == "deny"


def test_grants_override_replaces_the_preset_rows() -> None:
    assert _decide("toddler") == "deny"
