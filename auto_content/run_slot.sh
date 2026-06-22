#!/bin/bash
# Candor Content Autopilot — top the carousel queue back up to BUFFER_TARGET (~15)
# and text a "slides are ready" reminder (no links). Railway serves the queue +
# the Make buttons release from it, so posting keeps working with the Mac off; the
# Mac just replenishes the buffer when it's on. Secrets live in
# ~/.candor_autopilot.env (NOT in git).
source "$HOME/.candor_autopilot.env"
cd "$HOME/college-tool" || exit 1
LOG="$HOME/Library/Logs/candor-autopilot.log"
# One-day skip guard: if ~/.candor_skip_slot holds this exact YYYY-MM-DD-HH, skip
# this slot once (used to move a single day's slot to a manual one-shot time).
SKIP_FILE="$HOME/.candor_skip_slot"
if [ -f "$SKIP_FILE" ] && [ "$(cat "$SKIP_FILE" 2>/dev/null)" = "$(date +%Y-%m-%d-%H)" ]; then
  echo "=== $(date) slot SKIPPED (skip-file) ===" >> "$LOG"; rm -f "$SKIP_FILE"; exit 0
fi
echo "=== $(date) buffer top-up ===" >> "$LOG"
/usr/bin/python3 auto_content/factory.py >> "$LOG" 2>&1
echo "exit=$? $(date)" >> "$LOG"
