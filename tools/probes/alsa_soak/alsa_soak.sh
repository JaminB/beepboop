#!/usr/bin/env sh
# V4: raw ALSA capture in the audio container for 24 h, counting xruns and device-busy errors.
# Standard POSIX sh + alsa-utils; runs on the Pi host or inside the audio container
# (`docker compose exec audio sh /probes/alsa_soak.sh`). Writes a JSON summary next to the
# bench results. Audio goes to /dev/null: nothing is persisted (ADR 0018).
#
#   alsa_soak.sh [-D hw:1] [-d 86400] [-o tools/bench/results/<date>-pi4-4gb-v4.json] [-v variant]
#
# Pass: 24 h without "Device or resource busy" and xruns <= 1/h (ROADMAP V4).
# The variant run (same 24 h with SSD write load + a `docker pull`) is driven by run_variant.sh.

set -u
DEVICE="hw:1"
DURATION=86400
OUT=""
VARIANT="plain"
while getopts "D:d:o:v:" opt; do
  case "$opt" in
    D) DEVICE="$OPTARG" ;;
    d) DURATION="$OPTARG" ;;
    o) OUT="$OPTARG" ;;
    v) VARIANT="$OPTARG" ;;
    *) echo "usage: $0 [-D device] [-d seconds] [-o out.json] [-v variant]" >&2; exit 2 ;;
  esac
done
HOST="$(hostname)"
DATE="$(date -u +%Y-%m-%d)"
[ -n "$OUT" ] || OUT="tools/bench/results/${DATE}-${HOST}-v4-${VARIANT}.json"
LOG="$(mktemp)"
START="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
T0=$(date +%s)

# Same stream shape as the product (VOICE_PIPELINE §2.3): S16_LE, 16 kHz, 6 ch, 160-frame period.
arecord -v -D "$DEVICE" -f S16_LE -r 16000 -c 6 --period-size=160 --buffer-size=640 \
  -t raw -d "$DURATION" /dev/null 2>"$LOG"
RC=$?
T1=$(date +%s)
ELAPSED=$((T1 - T0))

XRUNS=$(grep -c -i "overrun\|underrun\|xrun" "$LOG")
BUSY=$(grep -c -i "busy" "$LOG")
ERRORS=$(grep -c -i "error" "$LOG")
HOURS=$(awk "BEGIN { printf \"%.3f\", $ELAPSED / 3600 }")
XRUNS_PER_H=$(awk "BEGIN { if ($ELAPSED > 0) printf \"%.4f\", $XRUNS / ($ELAPSED / 3600); else print \"null\" }")
# Pass only if the full duration ran and the thresholds hold.
if [ "$RC" -eq 0 ] && [ "$BUSY" -eq 0 ] && [ "$ELAPSED" -ge "$DURATION" ] && \
   awk "BEGIN { exit !($XRUNS_PER_H <= 1.0) }"; then PASS=true; else PASS=false; fi

mkdir -p "$(dirname "$OUT")"
cat >"$OUT" <<EOF
{
  "item": "V4",
  "variant": "$VARIANT",
  "host": "$HOST",
  "device": "$DEVICE",
  "started_at": "$START",
  "duration_requested_s": $DURATION,
  "duration_actual_s": $ELAPSED,
  "hours": $HOURS,
  "arecord_exit": $RC,
  "xruns": $XRUNS,
  "xruns_per_hour": $XRUNS_PER_H,
  "device_busy_errors": $BUSY,
  "other_error_lines": $ERRORS,
  "pass": $PASS,
  "rule": "ROADMAP V4: 24 h, no device-busy errors, xruns <= 1/h",
  "kernel": "$(uname -r)",
  "log_tail": $(tail -n 5 "$LOG" | sed 's/"/\\"/g' | awk 'BEGIN{printf "["} {printf "%s\"%s\"", (NR>1?",":""), $0} END{printf "]"}')
}
EOF
rm -f "$LOG"
echo "wrote $OUT (pass=$PASS, xruns=$XRUNS over ${HOURS} h, busy=$BUSY)"
[ "$PASS" = true ]
