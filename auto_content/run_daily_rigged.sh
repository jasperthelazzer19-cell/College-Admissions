#!/bin/bash
# Daily hot-take batch. Runs every morning at 6:40 (com.candor.dailyrigged).
#
# WAS one-shot test mode: it unloaded its own launchd job after the first
# successful run, which made sense while the format was being trialled and
# nothing else. Left in place it meant the multi-angle rotation added on
# 2026-07-26 (round / legacy, and the celeb tracks behind it) would generate
# exactly one morning's batch and then silently stop forever. The daily stamp
# below is the real re-entry guard, so the self-unload was redundant as well as
# harmful. Removed.
source "$HOME/.candor_autopilot.env"
cd "$HOME/college-tool" || exit 1
LOG="$HOME/Library/Logs/candor-dailyrigged.log"
STAMP="$HOME/.candor_rigged_day"
TODAY=$(date +%Y-%m-%d)
if [ -f "$STAMP" ] && [ "$(cat "$STAMP" 2>/dev/null)" = "$TODAY" ]; then
  exit 0
fi
echo "=== $(date) daily hot-take batch ===" >> "$LOG"
/usr/bin/python3 auto_content/make_daily_rigged.py --n 8 >> "$LOG" 2>&1
rc=$?
if [ $rc -eq 0 ]; then
  echo "$TODAY" > "$STAMP"
fi
echo "exit=$rc $(date)" >> "$LOG"
