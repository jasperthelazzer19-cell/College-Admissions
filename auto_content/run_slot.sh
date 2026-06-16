#!/bin/bash
# Candor Content Autopilot — one slot: generate a carousel + text its link.
# Secrets live in ~/.candor_autopilot.env (NOT in git).
source "$HOME/.candor_autopilot.env"
cd "$HOME/Desktop/college-tool" || exit 1
LOG="$HOME/Library/Logs/candor-autopilot.log"
# One-day skip guard: if ~/.candor_skip_slot holds this exact YYYY-MM-DD-HH, skip
# this slot once (used to move a single day's slot to a manual one-shot time).
SKIP_FILE="$HOME/.candor_skip_slot"
if [ -f "$SKIP_FILE" ] && [ "$(cat "$SKIP_FILE" 2>/dev/null)" = "$(date +%Y-%m-%d-%H)" ]; then
  echo "=== $(date) slot SKIPPED (skip-file) ===" >> "$LOG"; rm -f "$SKIP_FILE"; exit 0
fi
echo "=== $(date) slot run ===" >> "$LOG"
/usr/bin/python3 auto_content/factory.py --slot >> "$LOG" 2>&1
echo "exit=$? $(date)" >> "$LOG"
