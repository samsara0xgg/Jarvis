#!/bin/bash
# Bring the Pi brain up to its origin's main and restart it while he is not talking.
#
#   ~/jarvis-brain/scripts/deploy-brain.sh            deploy origin/main (the Pi bare repo's main)
#   ~/jarvis-brain/scripts/deploy-brain.sh --dry-run  run every check, change nothing
#
# Runs on the Pi, in ~/jarvis-brain. One deploy at a time (flock on ~/.jarvis/deploy.lock); a
# second one exits at once. The checkout only fast-forwards; uv sync runs only when uv.lock moved.
# The restart waits, up to 30 minutes, until no turn in the last 60 s and no restart in the last
# 5 minutes. Between 04:00 and 23:30 it does not restart while today has no plan, since a fresh
# start would draft one at once and push it to his phone (ADR 0288). The code stays pulled when it
# does not restart. Reads no key and prints none.
set -euo pipefail

REPO="$HOME/jarvis-brain"
EVENTS="$HOME/.jarvis/mac_events.db"
HEALTH="http://127.0.0.1:8006/api/health"
WAIT_S=1800
DRY=0
case "${1:-}" in
    "") ;;
    --dry-run) DRY=1 ;;
    *) echo "usage: $0 [--dry-run]" >&2; exit 2 ;;
esac

exec 9>"$HOME/.jarvis/deploy.lock"
flock -n 9 || { echo "deploy: another deploy holds ~/.jarvis/deploy.lock" >&2; exit 1; }
cd "$REPO"

# Seconds since the newest turn, and whether today (local) has a plan or a failed draft.
quiet_s() {
    python3 -I - "$EVENTS" <<'EOF'
import sqlite3, sys, time
c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
r = c.execute("SELECT MAX(ts_epoch_ms) FROM events WHERE type IN ('utterance.received',"
              "'surface.user_intent','turn.started','surface.playback_started')").fetchone()[0]
print(int(time.time() - r / 1000) if r else 10**9)
EOF
}
has_plan() {
    python3 -I - "$EVENTS" "$(date +%F)" <<'EOF'
import json, sqlite3, sys
c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
rows = c.execute("SELECT payload_json FROM events WHERE type IN ('plan.drafted','plan.draft_failed')"
                 " ORDER BY id DESC LIMIT 50")
print("yes" if any(sys.argv[2] in (d.get("day"), d.get("date")) for d in map(json.loads, (r[0] for r in rows))) else "no")
EOF
}
restart_age_s() {
    local since up
    since=$(systemctl --user show jarvis-brain -p ActiveEnterTimestampMonotonic --value)
    up=$(cut -d' ' -f1 /proc/uptime)
    echo $(( ${up%.*} - since / 1000000 ))
}

git fetch -q origin main
old=$(git rev-parse --short HEAD)
new=$(git rev-parse --short origin/main)
git merge-base --is-ancestor HEAD origin/main || { echo "deploy: $old does not fast-forward to $new" >&2; exit 1; }
if [ "$DRY" = 1 ]; then
    echo "dry-run: $old -> $new ($(git rev-list --count HEAD..origin/main) commits)," \
         "uv.lock $(git diff --quiet HEAD origin/main -- uv.lock && echo same || echo changed)," \
         "quiet $(quiet_s) s, last restart $(restart_age_s) s ago, plan today: $(has_plan)"
    exit 0
fi

git merge -q --ff-only origin/main
if ! git diff --quiet "$old" HEAD -- uv.lock; then
    "$HOME/.local/bin/uv" sync -q --frozen --inexact --no-build
fi

now=$(date +%H%M)
if [ "$now" -ge 0400 ] && [ "$now" -lt 2330 ] && [ "$(has_plan)" = no ]; then
    echo "deploy: $old -> $new pulled, not restarted: today has no plan yet and a start would draft one"
    exit 0
fi

waited=0
until [ "$(quiet_s)" -ge 60 ] && [ "$(restart_age_s)" -ge 300 ]; do
    if [ "$waited" -ge "$WAIT_S" ]; then
        echo "deploy: $old -> $new pulled, not restarted: no quiet minute in 30 min"
        exit 0
    fi
    sleep 15
    waited=$((waited + 15))
done

systemctl --user restart jarvis-brain
at=$(date +%T)
code=000
for _ in $(seq 60); do
    code=$(curl -s -o /dev/null -w '%{http_code}' "$HEALTH" || true)
    [ "$code" = 200 ] && break
    sleep 1
done
echo "deploy: $old -> $(git rev-parse --short HEAD), restarted $at, health $code"
[ "$code" = 200 ]
