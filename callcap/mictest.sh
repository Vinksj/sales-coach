#!/bin/bash
# Dual-channel capture check. Phase 1 settles the macOS permission prompts
# (an ad-hoc signature means every rebuild resets them); phase 2 records for
# real and reports per-channel sample counts, so a silent channel is obvious.
cd "$(dirname "$0")/.."
BIN=callcap/build/callcap
ERR=/tmp/callcap-mictest.err

echo "Phase 1: permissions. Click Allow on any prompt that appears."
"$BIN" --duration 2 >/dev/null 2>"$ERR.p1"
grep -o '"mic_authorization":"[a-z_]*"' "$ERR.p1" | tail -1

echo
echo "Phase 2: recording 12s. Speak normally after the beep, in English and Hindi."
( sleep 2; afplay /System/Library/Sounds/Ping.aiff 2>/dev/null
  sleep 2; say "testing one two three, kya yeh kaam kar raha hai" ) &
"$BIN" --duration 12 >/dev/null 2>"$ERR"
wait

echo
echo "=== events ==="
grep '"event"' "$ERR" | grep -v '"heartbeat"'
echo "=== final heartbeat ==="
grep '"heartbeat"' "$ERR" | tail -1
echo
echo "Full log: $ERR"
