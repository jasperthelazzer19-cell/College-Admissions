#!/bin/bash
# Daily RIGGED batch — ONE-SHOT TEST MODE: runs once (tomorrow), then unloads
# its own launchd job. Re-arm later with:
#   launchctl load ~/Library/LaunchAgents/com.candor.dailyrigged.plist
source "$HOME/.candor_autopilot.env"
cd "$HOME/college-tool" || exit 1
LOG="$HOME/Library/Logs/candor-dailyrigged.log"
STAMP="$HOME/.candor_rigged_day"
TODAY=$(date +%Y-%m-%d)
if [ -f "$STAMP" ] && [ "$(cat "$STAMP" 2>/dev/null)" = "$TODAY" ]; then
  exit 0
fi
echo "=== $(date) daily rigged batch (one-shot test) ===" >> "$LOG"
/usr/bin/python3 auto_content/make_daily_rigged.py --n 8 >> "$LOG" 2>&1
rc=$?
if [ $rc -eq 0 ]; then
  echo "$TODAY" > "$STAMP"
  # one-shot: disarm after the successful test run
  launchctl unload "$HOME/Library/LaunchAgents/com.candor.dailyrigged.plist" 2>/dev/null &
  echo "one-shot complete — launchd job unloaded" >> "$LOG"
fi
echo "exit=$rc $(date)" >> "$LOG"
