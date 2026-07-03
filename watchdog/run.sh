#!/bin/zsh
# Candor watchdog tick — launchd entry point (every 2 min).
set -a; source "$HOME/.candor_autopilot.env" 2>/dev/null; set +a
export PATH="$HOME/.nvm/versions/node/v25.9.0/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"
cd "$HOME/college-tool/watchdog"
/usr/bin/python3 monitor.py >> tick.log 2>&1
