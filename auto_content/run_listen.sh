#!/bin/bash
# Text-to-make watcher: check for new iMessage requests and fulfill them.
source "$HOME/.candor_autopilot.env"
cd "$HOME/college-tool" || exit 1
/usr/bin/python3 auto_content/text_listen.py >> "$HOME/Library/Logs/candor-autopilot.log" 2>&1
