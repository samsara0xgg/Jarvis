#!/usr/bin/env bash
# Bootstrap and start the Jarvis companion demo on macOS.
#
#   scripts/try-demo.sh [--yes] [--dry-run] [--full]
#
#   --yes      install missing prerequisites without asking
#   --dry-run  print what is missing and the install plan, then exit
#   --full     also prepare the full run (uv + `uv sync`); prints the start
#              commands instead of launching anything
#
# Requirements (see desktop/resonance/scripts/build-native.mjs): macOS, Node 24
# with C headers (node_api.h), Xcode Command Line Tools (clang++, git).
# Never uses sudo; Homebrew's own installer asks for the password itself.
set -euo pipefail

YES=0 DRY=0 FULL=0
for arg in "$@"; do
  case "$arg" in
    --yes) YES=1 ;;
    --dry-run) DRY=1 ;;
    --full) FULL=1 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BREW_INSTALL='/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'

have() { command -v "$1" >/dev/null 2>&1; }

# Succeeds if $1 is a node binary >= 24 whose C headers build-native.mjs can find.
node_ok() {
  [ -n "$1" ] && [ -x "$1" ] || return 1
  local major bindir d
  major=$("$1" -p 'process.versions.node.split(".")[0]' 2>/dev/null) || return 1
  [ "$major" -ge 24 ] || return 1
  bindir=$(dirname "$("$1" -p process.execPath)")
  for d in "${NODE_INCLUDE:-}" "$bindir/../include/node" /opt/homebrew/include/node /usr/local/include/node; do
    if [ -n "$d" ] && [ -f "$d/node_api.h" ]; then return 0; fi
  done
  return 1
}

if [ "$(uname -s)" != "Darwin" ]; then
  echo "This demo needs macOS (it builds a native Cocoa module). Detected: $(uname -s)." >&2
  exit 1
fi

echo "Jarvis demo bootstrap - checking prerequisites:"
echo "  macOS, git, Xcode Command Line Tools, Node >= 24 with C headers$([ "$FULL" = 1 ] && echo ', uv')"
echo
echo "Found:"
case "$(uname -m)" in
  arm64) echo "  [ok] macOS on Apple Silicon" ;;
  *) echo "  [ok] macOS on Intel (note: the demo is built and tested on Apple Silicon)" ;;
esac

# Command Line Tools provide clang++ and git (the /usr/bin/git stub only works with them).
NEED_CLT=0 NEED_BREW=0 NEED_NODE=0 NEED_UV=0
if xcode-select -p >/dev/null 2>&1; then
  echo "  [ok] Xcode Command Line Tools ($(xcode-select -p))"
else
  NEED_CLT=1
fi
if [ "$NEED_CLT" = 0 ] || { have git && [ "$(command -v git)" != /usr/bin/git ]; }; then
  echo "  [ok] git"
fi

have brew || for p in /opt/homebrew/bin/brew /usr/local/bin/brew; do
  if [ -x "$p" ]; then eval "$("$p" shellenv)"; break; fi
done

NODE_BIN=$(command -v node || true)
if node_ok "$NODE_BIN"; then
  echo "  [ok] Node $("$NODE_BIN" -v) with C headers"
else
  KEG=""
  if have brew; then KEG="$(brew --prefix node@24 2>/dev/null)/bin"; fi
  if node_ok "$KEG/node"; then
    PATH="$KEG:$PATH"  # node@24 is keg-only (not linked), so use it for this run
    echo "  [ok] Node $(node -v) with C headers (Homebrew node@24, used for this run only)"
  else
    NEED_NODE=1
  fi
fi

if [ "$FULL" = 1 ]; then
  if have uv; then echo "  [ok] uv $(uv --version | cut -d' ' -f2)"; else NEED_UV=1; fi
fi

if [ "$NEED_NODE" = 1 ] || [ "$NEED_UV" = 1 ]; then
  have brew || NEED_BREW=1
fi

if [ $((NEED_CLT + NEED_BREW + NEED_NODE + NEED_UV)) = 0 ]; then
  echo; echo "Missing: nothing. Nothing to install."
else
  echo; echo "Missing, and how it will be installed:"
  if [ "$NEED_CLT" = 1 ]; then echo "  - Xcode Command Line Tools (also provide git): xcode-select --install"; fi
  if [ "$NEED_BREW" = 1 ]; then echo "  - Homebrew: $BREW_INSTALL"; fi
  if [ "$NEED_NODE" = 1 ]; then echo "  - Node.js 24 with headers: brew install node@24 (keg-only; this script puts it on PATH for the run)"; fi
  if [ "$NEED_UV" = 1 ]; then echo "  - uv: brew install uv"; fi
fi

if [ -f "$ROOT/.mise.toml" ] && have mise; then
  echo
  echo "Note: mise detected. If it reports an untrusted .mise.toml, review it and run 'mise trust' yourself; this script never does."
fi

if [ "$DRY" = 1 ]; then
  echo; echo "Dry run: nothing installed or started."
  exit 0
fi

if [ $((NEED_CLT + NEED_BREW + NEED_NODE + NEED_UV)) != 0 ]; then
  if [ "$YES" = 0 ]; then
    printf '\nInstall these now? [y/N] '
    read -r ans || ans=n
    case "$ans" in y|Y|yes|YES) ;; *) echo "Aborted; nothing installed."; exit 1 ;; esac
  fi

  if [ "$NEED_CLT" = 1 ]; then
    xcode-select --install || true
    echo "A system dialog opened. Finish the system installer, then run this script again."
    exit 0
  fi
  if [ "$NEED_BREW" = 1 ]; then
    eval "$BREW_INSTALL"
    for p in /opt/homebrew/bin/brew /usr/local/bin/brew; do
      if [ -x "$p" ]; then eval "$("$p" shellenv)"; break; fi
    done
  fi
  if [ "$NEED_NODE" = 1 ]; then
    brew install node@24
    PATH="$(brew --prefix node@24)/bin:$PATH"
  fi
  if [ "$NEED_UV" = 1 ]; then brew install uv; fi
fi

if [ "$FULL" = 1 ]; then
  cd "$ROOT"
  uv sync
  cat <<EOF

Full run is prepared. Start it in two terminals:
  1. Daemon:    cd "$ROOT" && uv run python -m jarvis serve
  2. Companion: cd "$ROOT/desktop/resonance" && npm run companion
EOF
  exit 0
fi

cd "$ROOT/desktop/resonance"
npm ci
exec npm run companion -- --demo
