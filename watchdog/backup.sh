#!/bin/zsh
# Nightly Candor DB backup -> ~/CandorBackups (keep 14).
# The prod DB is ~1.3GB but 96% is regenerable carousel slide images in
# content_queue; we snapshot, NULL any >50KB text column in content_queue
# (keeps queue metadata), VACUUM, and dump the rest (~tens of MB).
setopt null_glob
set -a; source "$HOME/.candor_autopilot.env" 2>/dev/null; set +a
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"
# launchd hands this script a bare PATH (/usr/bin:/bin:/usr/sbin:/sbin), so the
# railway CLI — installed under nvm — was invisible and every nightly run since
# 2026-07-04 produced a 0-byte dump. Add every nvm node bin dir, newest first.
for _nvmbin in $HOME/.nvm/versions/node/*/bin(Nn); do export PATH="$_nvmbin:$PATH"; done
RAILWAY=$(command -v railway)
if [ -z "$RAILWAY" ]; then
  echo "$(date) BACKUP FAILED — railway CLI not on PATH ($PATH)" >> "$HOME/CandorBackups/backup.log"
  exit 1
fi
cd "$HOME/college-tool"
STAMP=$(date +%Y%m%d-%H%M)
OUT="$HOME/CandorBackups/college-$STAMP.sql.gz"
PYB64="aW1wb3J0IHNxbGl0ZTMsIHN5cwpzcmMgPSBzcWxpdGUzLmNvbm5lY3QoIi9hcHAvZGF0YS9jb2xsZWdlLmRiIikKZHN0ID0gc3FsaXRlMy5jb25uZWN0KCIvdG1wL2NhbmRvcl9iay5kYiIpCnNyYy5iYWNrdXAoZHN0KQpzcmMuY2xvc2UoKQpjb2xzID0gW3JbMV0gZm9yIHIgaW4gZHN0LmV4ZWN1dGUoIlBSQUdNQSB0YWJsZV9pbmZvKGNvbnRlbnRfcXVldWUpIildCmZvciBjb2wgaW4gY29sczoKICAgIHRyeToKICAgICAgICBteCA9IGRzdC5leGVjdXRlKGYnU0VMRUNUIE1BWChMRU5HVEgoIntjb2x9IikpIEZST00gY29udGVudF9xdWV1ZScpLmZldGNob25lKClbMF0gb3IgMAogICAgICAgIGlmIG14ID4gNTAwMDA6CiAgICAgICAgICAgIGRzdC5leGVjdXRlKGYnVVBEQVRFIGNvbnRlbnRfcXVldWUgU0VUICJ7Y29sfSI9TlVMTCcpCiAgICBleGNlcHQgRXhjZXB0aW9uOgogICAgICAgIHBhc3MKZHN0LmNvbW1pdCgpCmRzdC5leGVjdXRlKCJWQUNVVU0iKQpmb3IgbGluZSBpbiBkc3QuaXRlcmR1bXAoKToKICAgIHN5cy5zdGRvdXQud3JpdGUobGluZSArICJcbiIpCg=="
# Keep stderr — swallowing it is what hid the missing-CLI failure for 23 nights.
ERRLOG="$HOME/CandorBackups/.last-stderr.log"
"$RAILWAY" ssh "echo $PYB64 | base64 -d | python3 | gzip | base64" 2>"$ERRLOG" \
  | grep -E '^[A-Za-z0-9+/=]+$' | base64 -d > "$OUT"
"$RAILWAY" ssh "rm -f /tmp/candor_bk.db" >/dev/null 2>&1
SIZE=$(stat -f%z "$OUT" 2>/dev/null || echo 0)
if [ "$SIZE" -gt 300000 ] && gzip -t "$OUT" 2>/dev/null \
   && gunzip -c "$OUT" 2>/dev/null | head -80 | grep -q "CREATE TABLE"; then
  echo "$(date) backup ok: $OUT ($(du -h "$OUT" | cut -f1))" >> "$HOME/CandorBackups/backup.log"
else
  echo "$(date) BACKUP FAILED (size=$SIZE) — kept as $OUT.bad :: $(tail -2 "$ERRLOG" 2>/dev/null | tr '\n' ' ')" >> "$HOME/CandorBackups/backup.log"
  mv -f "$OUT" "$OUT.bad" 2>/dev/null
  osascript -e "tell application \"Messages\"
 set svc to 1st service whose service type = iMessage
 set b to buddy \"$PHONE\" of svc
 send \"⚠️ Candor nightly DB backup FAILED (size=$SIZE) — see ~/CandorBackups/backup.log\" to b
end tell" 2>/dev/null
fi
KEEP=($(ls -t "$HOME/CandorBackups"/college-*.sql.gz 2>/dev/null | tail -n +15))
[ ${#KEEP[@]} -gt 0 ] && rm -f "${KEEP[@]}"
