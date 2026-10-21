#!/bin/bash
# Bring the Pi brain up to its origin's main and restart it while he is not talking.
#
#   ~/jarvis-brain/scripts/deploy-brain.sh            deploy origin/main (the Pi bare repo's main)
#   ~/jarvis-brain/scripts/deploy-brain.sh --dry-run  run every check, change nothing
#
# Runs on the Pi, in ~/jarvis-brain. One deploy at a time (flock on ~/.jarvis/deploy.lock); a
# second one exits at once. The checkout only fast-forwards; uv sync runs only when uv.lock moved.
# Before anything restarts, `jarvis serve --check-config` loads ~/.jarvis's config the way the unit
# will (the new code, settings.yaml and the Settings store over the shipped file; ADR 0370). A
# config it refuses puts the checkout (and the venv, if uv.lock had moved) back where it was and
# exits 1 without restarting: the old brain keeps running. --dry-run runs the same check on the new
# tree exported to a temporary directory with the same venv, so the checkout does not move; when
# uv.lock changed the venv lacks the new dependencies, so it says the config was not checked.
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
# Whether the config loads the way jarvis-brain.service will load it: the unit's environment, the
# venv's interpreter, run from the tree $1 (so its code, not the checkout's, is imported). Prints
# the check's output; fails when the config is refused. `-m` puts the working directory first on
# sys.path, ahead of the editable install. $2... are extra environment assignments.
check_config() {
    local tree=$1
    shift
    (cd "$tree" && env JARVIS_RUNTIME_ROOT="$HOME/.jarvis" SQLITE_TMPDIR=/tmp "$@" \
        "$REPO/.venv/bin/python" -m jarvis serve --check-config 2>&1)
}
# The line that says why: the loader's own, since a warning can come before it; the last line when
# there is none (an interpreter that cannot start). At most 300 characters; never a key.
refusal_of() {
    { printf '%s\n' "$1" | grep -m1 '^jarvis serve: ' || printf '%s\n' "$1" | tail -n 1; } | cut -c1-300
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
    config="config ok"
    if ! git diff --quiet HEAD origin/main -- uv.lock; then
        config="config not checked (uv.lock changed: the new dependencies are not installed yet)"
    else
        tree=$(mktemp -d "${TMPDIR:-/tmp}/deploy-check.XXXXXX")
        trap 'rm -rf "$tree"' EXIT
        git archive origin/main jarvis config prompts data | tar -x -C "$tree"
        if ! out=$(check_config "$tree" PYTHONDONTWRITEBYTECODE=1); then
            echo "deploy: $old -> $new refused: config does not parse ($(refusal_of "$out"))" >&2
            exit 1
        fi
    fi
    echo "dry-run: $old -> $new ($(git rev-list --count HEAD..origin/main) commits)," \
         "uv.lock $(git diff --quiet HEAD origin/main -- uv.lock && echo same || echo changed)," \
         "$config, quiet $(quiet_s) s, last restart $(restart_age_s) s ago, plan today: $(has_plan)"
    exit 0
fi

old_head=$(git rev-parse HEAD)
git merge -q --ff-only origin/main
lock_moved=0
git diff --quiet "$old_head" HEAD -- uv.lock || lock_moved=1
if [ "$lock_moved" = 1 ]; then
    "$HOME/.local/bin/uv" sync -q --frozen --inexact --no-build
fi
if ! out=$(check_config "$REPO"); then
    git reset -q --hard "$old_head"
    if [ "$lock_moved" = 1 ]; then
        "$HOME/.local/bin/uv" sync -q --frozen --inexact --no-build \
            || echo "deploy: warning: uv sync back to $old failed" >&2
    fi
    echo "deploy: $old -> $new refused: config does not parse ($(refusal_of "$out"))" >&2
    exit 1
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
