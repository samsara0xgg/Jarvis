#!/usr/bin/env bash
# Jarvis foundation self-check — run this first in any fresh checkout or worktree.
#
# Green here means the environment is sound, so a red Tier 1 gate
# (lint-imports / ruff / mypy --strict / acceptance) is real code breakage and
# not environment drift. Every check prints the exact repair; `--fix` applies
# the ones that can be applied without a decision.
set -uo pipefail

ROOT="$(git rev-parse --show-toplevel)" || exit 1
cd "$ROOT"

FIX=0
[ "${1:-}" = "--fix" ] && FIX=1

FAILED=0
# sherpa-onnx is deliberately absent from uv.lock — pyproject keeps the voice
# wheels out of [project].dependencies so the daemon can downgrade to text-only
# (ADR-0005 §12). Consequence: every `uv sync` here must pass --inexact, or the
# sync uninstalls it as extraneous and the next daemon run is silently deaf.
# ponytail: bump this pin by hand when the ASR wheel moves.
SHERPA_PIN=1.13.2

ok()   { printf '  \033[32mOK\033[0m    %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n        fix: %s\n' "$1" "$2"; FAILED=1; }
note() { printf '        %s\n' "$1"; }

PRIMARY="$(dirname "$(cd "$(git rev-parse --git-common-dir)" && pwd)")"
printf '\njarvis init — %s (%s)\n' "$ROOT" "$(git rev-parse --abbrev-ref HEAD)"
[ "$ROOT" != "$PRIMARY" ] && printf 'linked worktree of %s\n' "$PRIMARY"
printf '\n'

# 1. mise trust — an untrusted .mise.toml means no python pin and no .venv.
if ! mise trust --show 2>/dev/null | grep -q ': untrusted'; then
    ok "mise config trusted"
elif [ "$FIX" = 1 ] && mise trust >/dev/null 2>&1; then
    ok "mise config trusted (fixed)"
else
    bad "mise config untrusted — every shell command in this tree errors, and .venv is never created" \
        "mise trust"
fi

# 2. venv exists and is Python 3.12.
PY=".venv/bin/python"
if [ -x "$PY" ] && "$PY" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 12))'; then
    ok "venv $( "$PY" -V )"
else
    if [ "$FIX" = 1 ]; then
        note "creating venv (uv sync --inexact --extra dev)…"
        uv sync --inexact --extra dev >/dev/null 2>&1 && ok "venv created ($( "$PY" -V ))" \
            || bad "venv missing or not Python 3.12" "uv sync --inexact --extra dev"
    else
        bad "venv missing or not Python 3.12" "uv sync --inexact --extra dev"
    fi
fi

# 3. `import jarvis` resolves to THIS checkout, not the primary one.
if [ -x "$PY" ]; then
    WHERE="$("$PY" -c 'import jarvis, pathlib; print(pathlib.Path(jarvis.__file__).parent)' 2>/dev/null)"
    if [ "$WHERE" = "$ROOT/jarvis" ]; then
        ok "import jarvis -> this checkout"
    else
        bad "import jarvis -> ${WHERE:-<not importable>} — edits here would not be what runs" \
            "uv sync --inexact --extra dev"
    fi
fi

# 4. Dev tools match uv.lock. An unlocked `uv pip install -e '.[dev]'` pulls a
#    newer ruff that reports hundreds of findings on a clean tree.
if [ -x "$PY" ]; then
    DRIFT=""
    for pkg in ruff mypy import-linter; do
        want="$(awk -v p="name = \"$pkg\"" '$0 == p { getline; gsub(/[^0-9.]/, "", $0); print; exit }' uv.lock)"
        have="$("$PY" -c "import importlib.metadata as m; print(m.version('$pkg'))" 2>/dev/null)"
        [ "$want" = "$have" ] || DRIFT="$DRIFT $pkg(lock=$want have=${have:-missing})"
    done
    if [ -z "$DRIFT" ]; then
        ok "dev tools match uv.lock (ruff $(.venv/bin/ruff --version | awk '{print $2}'))"
    elif [ "$FIX" = 1 ] && uv sync --inexact --frozen --extra dev >/dev/null 2>&1; then
        ok "dev tools resynced to uv.lock (fixed)"
    else
        bad "dev tools drifted from uv.lock:$DRIFT" "uv sync --inexact --frozen --extra dev"
    fi
fi

# 5. Voice wheels. Without them the daemon silently runs text-only: /inherent
#    ASR endpoints answer 500/501 and nothing says why.
if [ -x "$PY" ]; then
    missing_voice() {
        "$PY" - <<'PY' 2>/dev/null
import importlib.util
mods = ("sounddevice", "onnxruntime", "sherpa_onnx", "openwakeword", "soxr")
print(" ".join(m for m in mods if importlib.util.find_spec(m) is None))
PY
    }
    MISSING="$(missing_voice)"
    if [ -n "$MISSING" ] && [ "$FIX" = 1 ]; then
        note "installing voice wheels…"
        uv sync --inexact --frozen --extra dev >/dev/null 2>&1
        uv pip install "sherpa-onnx==$SHERPA_PIN" >/dev/null 2>&1
        MISSING="$(missing_voice)"   # re-probe: believe imports, not exit codes
    fi
    if [ -z "$MISSING" ]; then
        ok "voice stack importable (sherpa-onnx $("$PY" -c "import importlib.metadata as m; print(m.version('sherpa-onnx'))"))"
    else
        bad "voice stack incomplete — daemon would silently run text-only:$MISSING" \
            "uv sync --inexact --frozen --extra dev && uv pip install sherpa-onnx==$SHERPA_PIN"
    fi
fi

# 6. Model artifacts. .gitignore owns the list, so a new artifact is checked the
#    day it is ignored rather than the day someone remembers to edit this script.
for name in $(grep '^/data/' .gitignore | sed 's|^/data/||'); do
    if [ -e "data/$name" ]; then
        ok "data/$name resolves"
    else
        src="$PRIMARY/data/$name"
        target="$(readlink "$src" 2>/dev/null || echo "$src")"
        if [ ! -e "$src" ]; then
            bad "data/$name missing, and the primary checkout has none either" \
                "download the artifact, then symlink it into data/"
        elif [ "$FIX" = 1 ]; then
            ln -sfn "$target" "data/$name" && ok "data/$name linked -> $target (fixed)"
        else
            bad "data/$name missing" "ln -sfn $target data/$name"
        fi
    fi
done

# 7. The logging contract the run recipe below depends on. If this moves, the
#    recipe becomes a lie that silently produces empty logs.
if grep -q 'JARVIS_LOG_LEVEL' jarvis/__main__.py; then
    ok "JARVIS_LOG_LEVEL still gates logging setup"
else
    bad "jarvis/__main__.py no longer reads JARVIS_LOG_LEVEL" \
        "find the new switch and update the recipe at the bottom of scripts/init.sh"
fi

printf '\n'
if [ "$FAILED" = 1 ]; then
    printf 'Foundation RED. Re-run with --fix to apply the repairs above.\n\n'
    exit 1
fi

cat <<'RECIPE'
Foundation GREEN. Recipes for this checkout — Tier 1 gates verbatim from
docs/git-guide.md §1, whose exact forms are load-bearing (a bare `mypy .`
follows the data/ symlink this script just created into another checkout):

  .venv/bin/lint-imports
  .venv/bin/ruff check .
  .venv/bin/mypy --strict jarvis tests scripts tools
  .venv/bin/pytest tests -m "not live_llm and not live_codex" -x

  daemon   JARVIS_LOG_LEVEL=INFO env -u MINIMAX_API_KEY .venv/bin/python -m jarvis daemon run --force-manual
           (nothing is logged without JARVIS_LOG_LEVEL; --force-manual does NOT mute
            speech — unsetting MINIMAX_API_KEY does)
  desktop  cd desktop/resonance && node scripts/launch.mjs   (npm ci + build when stale)

RECIPE
