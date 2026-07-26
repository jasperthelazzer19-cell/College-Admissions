#!/bin/bash
# iMessage the creator a "time to post" nudge at each posting slot (8/12/3/6/9).
# Replaces the old hourly "slides are ready" buffer-topup text. Secrets (PHONE)
# live in ~/.candor_autopilot.env.
source "$HOME/.candor_autopilot.env" 2>/dev/null
[ -z "$PHONE" ] && { echo "PHONE not set — skipping"; exit 0; }

HOUR=$(date +%-H)
case "$HOUR" in
  8)  SLOT="morning (8am)";;
  12) SLOT="noon (12pm)";;
  15) SLOT="afternoon (3pm)";;
  18) SLOT="evening (6pm)";;
  21) SLOT="night (9pm)";;
  *)  SLOT="this";;
esac

MSG="⏰ Time to post the ${SLOT} Candor carousels."
/usr/bin/osascript -e "tell application \"Messages\"
 set svc to 1st service whose service type = iMessage
 set b to buddy \"${PHONE}\" of svc
 send \"${MSG}\" to b
end tell" && echo "posted reminder for $SLOT -> $PHONE"
