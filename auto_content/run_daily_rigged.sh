#!/bin/bash
# Daily RIGGED batch — one legacy hot-take carousel per account, rotating schools.
# Server sprinkles them across the day's slots (app._rigged_slot_today).
source "$HOME/.candor_autopilot.env"
cd "$HOME/college-tool" || exit 1
LOG="$HOME/Library/Logs/candor-dailyrigged.log"
# guard: only once per day even if launchd fires again (RunAtLoad after reboot etc.)
STAMP="$HOME/.candor_rigged_day"
TODAY=$(date +%Y-%m-%d)
if [ -f "$STAMP" ] && [ "$(cat "$STAMP" 2>/dev/null)" = "$TODAY" ]; then
  exit 0
fi
echo "=== $(date) daily rigged batch ===" >> "$LOG"
/usr/bin/python3 auto_content/make_daily_rigged.py --n 8 >> "$LOG" 2>&1
rc=$?
[ $rc -eq 0 ] && echo "$TODAY" > "$STAMP"
echo "exit=$rc $(date)" >> "$LOG"
