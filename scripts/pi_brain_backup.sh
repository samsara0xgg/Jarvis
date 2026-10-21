#!/bin/bash
# Daily copy of the Pi brain's SQLite databases to this Mac (the brain's SD card has no other copy).
#
#   scripts/pi_brain_backup.sh             back up now
#   scripts/pi_brain_backup.sh --install   run it daily at 04:00 from a LaunchAgent (a missed run fires at wake)
#
# Each *.db under the Pi's ~/.jarvis is copied, one database at a time, with SQLite's online backup
# API into the Pi's RAM (/dev/shm), pulled to ~/.jarvis/backups/pi-brain/<date>/, checked here with
# quick_check, and removed on the Pi before the next one starts. A database bigger than half of
# /dev/shm's free space is staged on the Pi's disk beside ~/.jarvis (~/.jarvis-backup-stage-<date>)
# instead of in RAM, and removed right after the pull. Only *.db files and the nightly event-log
# reports (~/.jarvis/logs/check-log-*.json, ADR 0324; ids, counts and times, no content) travel: env,
# tokens and every other file stay on the Pi. The newest 7 days are kept. A day's folder is itself a
# runtime root for `jarvis check-log --runtime-root <folder>`.
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
shm_stage="/dev/shm/jarvis-backup-$day"
disk_stage="\$HOME/.jarvis-backup-stage-$day"
dest="$DEST_ROOT/$day"
mkdir -p "$dest"
echo "$(date '+%F %T') backup start -> $dest"

# Runs on the Pi: "list" prints each database's path under ~/.jarvis; "stage DAY PATH" backs one
# database up into RAM, or onto the Pi's disk when it is more than half of /dev/shm's free space,
# and prints where the copy is.
read -r -d '' PI_PY <<'PY' || true
import pathlib, shutil, sqlite3, sys
root = pathlib.Path.home() / ".jarvis"
if sys.argv[1] == "list":
    for db in sorted(p for p in root.rglob("*.db") if p.is_file() and len(p.relative_to(root).parts) <= 4):
        print(db.relative_to(root).as_posix())
else:
    day, db = sys.argv[2], root / sys.argv[3]
    wal = db.with_name(db.name + "-wal")
    size = db.stat().st_size + (wal.stat().st_size if wal.exists() else 0)
    shm = pathlib.Path("/dev/shm")
    in_ram = shm.is_dir() and size * 2 <= shutil.disk_usage(shm).free
    stage = shm / f"jarvis-backup-{day}" if in_ram else pathlib.Path.home() / f".jarvis-backup-stage-{day}"
    stage.mkdir(parents=True, exist_ok=True)
    out = stage / "__".join(db.relative_to(root).parts)
    src, dst = sqlite3.connect(f"file:{db}?mode=ro", uri=True), sqlite3.connect(out)
    src.backup(dst)
    dst.execute("pragma journal_mode=delete")  # one self-contained file, no -wal beside it
    dst.close(); src.close()
    print(out)
PY

pi() { ssh -o BatchMode=yes -o ConnectTimeout=20 "$HOST" python3 - "$@" <<<"$PI_PY"; }
# Whatever happens, nothing of ours stays on the Pi.
cleanup() { ssh -o BatchMode=yes -o ConnectTimeout=20 "$HOST" "rm -rf '$shm_stage' \"$disk_stage\"" || true; }
trap cleanup EXIT

list="$(pi list)"
[ -n "$list" ] || { echo "no databases found on $HOST" >&2; exit 1; }
count=0
# The list comes in on fd 3: ssh in the loop reads stdin and would swallow the rest of it.
while IFS= read -r rel <&3; do
    staged="$(pi stage "$day" "$rel")"
    scp -q -o BatchMode=yes "$HOST:$staged" "$dest/"
    ssh -o BatchMode=yes "$HOST" "rm -f '$staged'"
    f="$dest/$(basename "$staged")"
    [ "$(sqlite3 -readonly "$f" 'pragma quick_check;')" = ok ] || { echo "quick_check failed: $f" >&2; exit 1; }
    echo "$(date '+%F %T') $rel ok ($(du -h "$f" | cut -f1))"
    count=$((count + 1))
done 3<<<"$list"
want="$(wc -l <<<"$list" | tr -d ' ')"
[ "$count" = "$want" ] || { echo "backed up $count of $want databases" >&2; exit 1; }
# The reports of the Pi's nightly check (jarvis-check-log.timer). None yet, or a failed pull, is a
# note and not a failed backup: the databases above are what this script is for.
if scp -q -o BatchMode=yes "$HOST:.jarvis/logs/check-log-*.json" "$dest/"; then
    echo "$(date '+%F %T') check-log reports ok ($(ls -1 "$dest"/check-log-*.json | wc -l | tr -d ' '))"
else
    echo "$(date '+%F %T') no check-log reports pulled from $HOST (none yet, or the pull failed)" >&2
fi
# Keep the newest $KEEP days.
ls -1d "$DEST_ROOT"/????-??-?? | sort -r | tail -n +"$((KEEP + 1))" | while read -r old; do rm -rf "$old"; done
echo "$(date '+%F %T') backup done: $count file(s), $(du -sh "$dest" | cut -f1)"
