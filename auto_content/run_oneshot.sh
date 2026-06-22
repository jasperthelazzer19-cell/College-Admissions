#!/bin/bash
# One-off carousel batch (manually scheduled). Runs the same slot job once, then
# removes its own LaunchAgent so it never fires again.
source "$HOME/.candor_autopilot.env"
cd "$HOME/college-tool" || exit 1
LOG="$HOME/Library/Logs/candor-autopilot.log"
echo "=== $(date) ONESHOT run ===" >> "$LOG"
/usr/bin/python3 auto_content/factory.py --slot >> "$LOG" 2>&1
echo "oneshot exit=$? $(date)" >> "$LOG"
# self-remove so this is genuinely one-time
UID_N=$(id -u)
launchctl bootout gui/$UID_N/com.candor.oneshot 2>/dev/null
rm -f "$HOME/Library/LaunchAgents/com.candor.oneshot.plist"
