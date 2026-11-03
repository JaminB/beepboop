#!/usr/bin/env sh
# G7: sample the SoC temperature and throttle bits on the Pi HOST at 1 Hz while
# `beepboop bench thermal` (a 20-minute bench e2e) runs in the containers.
#
# `bench thermal` samples vcgencmd itself when it can, but inside the core container
# /dev/vchiq may not be mapped; this host-side sampler is the authoritative G7 record.
#
#   run_g7.sh [-d 1200] [-o tools/bench/results/<date>-<host>-g7.json] [-- <bench command>]
#
# Default bench command (runs in the core container via the host CLI):
#   beepboop bench thermal --duration 1200 --concurrency 2 --host pi4-4gb
# Pass: no 0x4 (currently throttled) bit in any sample over >= 1200 s (ROADMAP G7).

set -u
DURATION=1200
OUT=""
while getopts "d:o:" opt; do
  case "$opt" in
    d) DURATION="$OPTARG" ;;
    o) OUT="$OPTARG" ;;
    *) echo "usage: $0 [-d seconds] [-o out.json] [-- bench command]" >&2; exit 2 ;;
  esac
done
shift $((OPTIND - 1))
[ "${1:-}" = "--" ] && shift
HOST="$(hostname)"
DATE="$(date -u +%Y-%m-%d)"
[ -n "$OUT" ] || OUT="tools/bench/results/${DATE}-${HOST}-g7.json"
command -v vcgencmd >/dev/null 2>&1 || { echo "vcgencmd not found: this runs on the Pi host" >&2; exit 2; }

SAMPLES="$(mktemp)"
START="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
( while :; do
    T=$(vcgencmd measure_temp | sed 's/temp=//; s/.C$//')
    B=$(vcgencmd get_throttled | sed 's/throttled=//')
    echo "$(date +%s) $T $B" >>"$SAMPLES"
    sleep 1
  done ) &
SAMPLER=$!
trap 'kill $SAMPLER 2>/dev/null' EXIT INT TERM

if [ $# -gt 0 ]; then "$@"; else
  beepboop bench thermal --duration "$DURATION" --concurrency 2 --host "$HOST"
fi
BENCH_RC=$?
kill $SAMPLER 2>/dev/null
trap - EXIT

N=$(wc -l <"$SAMPLES")
SEEN_0X4=$(awk '{ if (and(strtonum($3), 4)) c++ } END { print c+0 }' "$SAMPLES")
UNION=$(awk 'BEGIN{u=0} { u = or(u, strtonum($3)) } END { printf "0x%x", u }' "$SAMPLES")
TMAX=$(awk 'BEGIN{m=0} { if ($2+0 > m) m = $2+0 } END { print m }' "$SAMPLES")
TMEAN=$(awk '{ s += $2 } END { if (NR) printf "%.1f", s/NR; else print "null" }' "$SAMPLES")
if [ "$N" -ge "$DURATION" ] && [ "$SEEN_0X4" -eq 0 ] && [ "$BENCH_RC" -eq 0 ]; then PASS=true; else PASS=false; fi

mkdir -p "$(dirname "$OUT")"
cat >"$OUT" <<EOF
{
  "item": "G7",
  "host": "$HOST",
  "started_at": "$START",
  "samples": $N,
  "duration_requested_s": $DURATION,
  "bench_exit": $BENCH_RC,
  "temp_c_max": $TMAX,
  "temp_c_mean": $TMEAN,
  "throttle_bits_union": "$UNION",
  "samples_with_0x4": $SEEN_0X4,
  "pass": $PASS,
  "rule": "ROADMAP G7: no 0x4 throttle bit during a 20-minute bench e2e with the reference fan case",
  "case": "FILL IN: fan case model",
  "kernel": "$(uname -r)",
  "firmware": "$(vcgencmd version | head -n 1)"
}
EOF
rm -f "$SAMPLES"
echo "wrote $OUT (pass=$PASS, samples=$N, max temp=${TMAX}C, 0x4 samples=$SEEN_0X4)"
[ "$PASS" = true ]
