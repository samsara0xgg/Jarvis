#!/bin/bash
# Daily copy of the Pi brain's SQLite databases to this Mac (the brain's SD card has no other copy).
#
#   scripts/pi_brain_backup.sh             back up now
#   scripts/pi_brain_backup.sh --install   run it daily at 04:00 from a LaunchAgent (a missed run fires at wake)
#
# Each *.db under the Pi's ~/.jarvis is copied with SQLite's online backup API into the Pi's RAM
# (/dev/shm, never the SD card), pulled to ~/.jarvis/backups/pi-brain/<date>/, then removed there.
# Only *.db files travel: env, tokens and every other file stay on the Pi. The newest 7 days are kept.
set -euo pipefail

HOST="${JARVIS_PI_HOST:-allen@jarvis}"
DEST_ROOT="$HOME/.jarvis/backups/pi-brain"
KEEP=7
LABEL="com.allen.jarvis.pi-backup"

if [ "${1:-}" = "--install" ]; then
    script="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
    plist="$HOME/Library/LaunchAgents/$LABEL.plist"
    mkdir -p "$DEST_ROOT"
    cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$script</string></array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>4</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>$DEST_ROOT/backup.log</string>
  <key>StandardErrorPath</key><string>$DEST_ROOT/backup.log</string>
</dict>
</plist>
EOF
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$plist"
    echo "installed $plist (daily 04:00)"
    exit 0
fi

day="$(date +%Y-%m-%d)"
stage="/dev/shm/jarvis-backup-$day"
dest="$DEST_ROOT/$day"
mkdir -p "$dest"
echo "$(date '+%F %T') backup start -> $dest"

ssh -o BatchMode=yes -o ConnectTimeout=20 "$HOST" "python3 - '$stage'" <<'PY'
import pathlib, sqlite3, sys
stage = pathlib.Path(sys.argv[1]); stage.mkdir(parents=True, exist_ok=True)
root = pathlib.Path.home() / ".jarvis"
for db in sorted(p for p in root.rglob("*.db") if p.is_file() and len(p.relative_to(root).parts) <= 3):
    out = stage / "__".join(db.relative_to(root).parts)
    src, dst = sqlite3.connect(f"file:{db}?mode=ro", uri=True), sqlite3.connect(out)
    src.backup(dst)
    dst.execute("pragma journal_mode=delete")  # one self-contained file, no -wal beside it
    dst.close(); src.close()
    print(out.name)
PY
scp -q -o BatchMode=yes "$HOST:$stage/*.db" "$dest/"
ssh -o BatchMode=yes "$HOST" "rm -rf '$stage'"

for f in "$dest"/*.db; do
    [ "$(sqlite3 -readonly "$f" 'pragma quick_check;')" = ok ] || { echo "quick_check failed: $f" >&2; exit 1; }
done
# Keep the newest $KEEP days.
ls -1d "$DEST_ROOT"/????-??-?? | sort -r | tail -n +"$((KEEP + 1))" | while read -r old; do rm -rf "$old"; done
echo "$(date '+%F %T') backup done: $(ls "$dest" | wc -l | tr -d ' ') file(s), $(du -sh "$dest" | cut -f1)"
