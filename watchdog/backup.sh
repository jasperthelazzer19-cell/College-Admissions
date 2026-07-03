#!/bin/zsh
# Nightly Candor DB backup: prod sqlite -> ~/CandorBackups, keep 14.
# Container has no sqlite3 CLI — dump via python3 iterdump.
set -a; source "$HOME/.candor_autopilot.env" 2>/dev/null; set +a
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"
cd "$HOME/college-tool"
STAMP=$(date +%Y%m%d-%H%M)
OUT="$HOME/CandorBackups/college-$STAMP.sql.gz"
railway ssh 'python3 -c "
import sqlite3, sys
c = sqlite3.connect(\"/app/data/college.db\")
for line in c.iterdump():
    sys.stdout.write(line + \"\n\")
" | gzip | base64' 2>/dev/null | grep -E '^[A-Za-z0-9+/=]+$' | base64 -d > "$OUT"
SIZE=$(stat -f%z "$OUT" 2>/dev/null || echo 0)
if [ "$SIZE" -gt 300000 ] && gzip -t "$OUT" 2>/dev/null \
   && gunzip -c "$OUT" | head -50 | grep -q "CREATE TABLE"; then
  echo "$(date) backup ok: $OUT ($(du -h "$OUT" | cut -f1))" >> "$HOME/CandorBackups/backup.log"
else
  echo "$(date) BACKUP FAILED (size=$SIZE)" >> "$HOME/CandorBackups/backup.log"
  osascript -e "tell application \"Messages\"
 set svc to 1st service whose service type = iMessage
 set b to buddy \"$PHONE\" of svc
 send \"⚠️ Candor nightly DB backup FAILED (size=$SIZE bytes) — check ~/CandorBackups/backup.log\" to b
end tell" 2>/dev/null
  rm -f "$OUT"
fi
ls -t "$HOME/CandorBackups"/college-*.sql.gz 2>/dev/null | tail -n +15 | xargs rm -f 2>/dev/null
