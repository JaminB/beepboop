#!/usr/bin/env bash
# M0 item V1 probe: Home Assistant must not be reachable from the LAN on :8123.
# See README.md in this directory for the full owner procedure.
#
#   On a SECOND machine on the LAN (Linux/macOS; on Windows run v1_report.py directly):
#     ./verify_v1.sh lan <pi-lan-ip> > lan-<scenario>.json
#
#   On the Pi, as root, from a checkout of the repo:
#     sudo ./verify_v1.sh pi --scenario both|bind-only|nft-only --lan-result lan-<scenario>.json
#
# The `pi` mode collects raw facts (ss, nft, docker) and hands them to v1_report.py,
# which merges the scenario into tools/bench/results/<date>-<host>-v1.json.
# Nothing is simulated: if a fact cannot be collected, it is recorded as null.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPORT="$HERE/v1_report.py"
RESULTS_DIR="$(cd "$HERE/../../bench" && pwd)/results"
HA_CONTAINER="${HA_CONTAINER:-homeassistant}"

die() { echo "verify_v1: $*" >&2; exit 2; }
warn() { echo "verify_v1: WARNING: $*" >&2; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing command: $1"; }

usage() { sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

cmd_lan() {
    [[ $# -ge 1 ]] || die "usage: verify_v1.sh lan <pi-lan-ip> [--control-port N] [--timeout S]"
    need python3
    exec python3 "$REPORT" lan "$@"
}

cmd_pi() {
    local scenario="" lan_result=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --scenario) scenario="${2:-}"; shift 2 ;;
            --lan-result) lan_result="${2:-}"; shift 2 ;;
            *) die "unknown argument: $1" ;;
        esac
    done
    [[ -n "$scenario" ]] || die "--scenario both|bind-only|nft-only is required"
    [[ $EUID -eq 0 ]] || die "run as root (nft list ruleset needs it): sudo $0 pi ..."
    need ss; need python3
    if [[ -z "$lan_result" ]]; then
        warn "no --lan-result: the LAN-side check is not recorded and the verdict stays INCOMPLETE"
    fi

    local tmp; tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT

    ss -Hltn > "$tmp/ss.txt"

    local nft_args=()
    if command -v nft >/dev/null 2>&1; then
        nft -j list ruleset > "$tmp/nft.json"
        nft_args=(--nft-file "$tmp/nft.json")
    else
        warn "nft not installed: nftables state recorded as null"
    fi

    local ha_version="" ha_image="" ha_image_id=""
    if command -v docker >/dev/null 2>&1 && docker inspect "$HA_CONTAINER" >/dev/null 2>&1; then
        ha_image="$(docker inspect --format '{{.Config.Image}}' "$HA_CONTAINER")"
        ha_image_id="$(docker inspect --format '{{.Image}}' "$HA_CONTAINER")"
        ha_version="$(docker exec "$HA_CONTAINER" python3 -c \
            'from homeassistant.const import __version__; print(__version__)' 2>/dev/null || true)"
    else
        warn "container '$HA_CONTAINER' not found: HA version recorded as null"
    fi

    local pi_model=""
    [[ -r /proc/device-tree/model ]] && pi_model="$(tr -d '\0' < /proc/device-tree/model)"

    # Sanity hints so a mis-set scenario is obvious (the verdict also refuses to use it).
    if [[ "$scenario" == "bind-only" ]] && grep -q 'table inet beepboop' <(nft list tables 2>/dev/null || true); then
        warn "scenario bind-only but table inet beepboop is loaded; remove it first (README step 3)"
    fi
    if ! grep -qE ':8123[[:space:]]' "$tmp/ss.txt"; then
        warn "nothing listens on :8123 -- is HA running? This run cannot judge either layer."
    elif [[ "$scenario" == "nft-only" ]] && ! grep -qvE '(127\.0\.0\.1|\[::1\]):8123[[:space:]]' <(grep -E ':8123[[:space:]]' "$tmp/ss.txt"); then
        warn "scenario nft-only but HA listens on loopback only; remove server_host first (README step 4)"
    fi

    python3 "$REPORT" pi \
        --scenario "$scenario" \
        --ss-file "$tmp/ss.txt" \
        "${nft_args[@]}" \
        ${lan_result:+--lan-result "$lan_result"} \
        --host "$(hostname -s)" \
        --ha-version "$ha_version" \
        --ha-image "$ha_image" \
        --ha-image-id "$ha_image_id" \
        --pi-model "$pi_model" \
        --results-dir "$RESULTS_DIR"
}

case "${1:-}" in
    lan) shift; cmd_lan "$@" ;;
    pi) shift; cmd_pi "$@" ;;
    -h|--help|help|"") usage ;;
    *) die "unknown mode '$1' (lan | pi | help)" ;;
esac
