#!/bin/bash
# Candor Content Autopilot — one slot: generate a carousel + text its link.
# Secrets live in ~/.candor_autopilot.env (NOT in git).
source "$HOME/.candor_autopilot.env"
cd "$HOME/Desktop/college-tool" || exit 1
LOG="$HOME/Library/Logs/candor-autopilot.log"
echo "=== $(date) slot run ===" >> "$LOG"
/usr/bin/python3 auto_content/factory.py --slot >> "$LOG" 2>&1
echo "exit=$? $(date)" >> "$LOG"
