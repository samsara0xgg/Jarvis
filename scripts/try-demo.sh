#!/usr/bin/env bash
# Try the Jarvis desktop companion on its built-in demo data (macOS only).
#
#   curl -fsSL https://raw.githubusercontent.com/samsara0xgg/Jarvis/main/scripts/try-demo.sh | bash
#
# Installs what is missing (Xcode Command Line Tools, Node.js 24), clones the
# repo, installs the npm dependencies and starts `npm run companion -- --demo`.
# Nothing is installed without asking first; no accounts, keys or paid APIs.
# The whole body lives in functions and main runs on the last line, so a
# truncated download never executes half a script.

set -euo pipefail

REPO_URL="https://github.com/samsara0xgg/Jarvis"
NODE_DIST="https://nodejs.org/dist/latest-v24.x"
NODE_MIN_MAJOR=24

ASSUME_YES=0
FULL=0
INSTALL_DIR="${JARVIS_DIR:-$HOME/Jarvis}"
NEED_CLT=0
NEED_NODE=0
NEED_UV=0
TMP_DIR=""

setup_colors() {
  if [ -t 1 ]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'
    GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
  else
    BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; RESET=""
  fi
}

say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✓%s %s\n' "$GREEN" "$RESET" "$*"; }
miss() { printf '%s✗%s %s\n' "$RED" "$RESET" "$*"; }
warn() { printf '%s!%s %s\n' "$YELLOW" "$RESET" "$*" >&2; }
die()  { printf '%s✗%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Try the Jarvis desktop companion on its built-in demo data (macOS only).

Usage: try-demo.sh [options]

  --yes         Don't ask; install whatever is missing.
  --dir <path>  Where to put Jarvis (default: $JARVIS_DIR or ~/Jarvis).
  --full        Also install uv and print the next steps for running Jarvis
                for real (the demo itself never needs uv).
  --help        Show this help.
EOF
}

cleanup() {
  if [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ]; then rm -rf "$TMP_DIR"; fi
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --yes|-y) ASSUME_YES=1 ;;
      --full)   FULL=1 ;;
      --dir)
        [ "$#" -ge 2 ] || die "--dir needs a path."
        INSTALL_DIR="$2"; shift ;;
      --dir=*)  INSTALL_DIR="${1#--dir=}" ;;
      --help|-h) usage; exit 0 ;;
      *) usage >&2; die "Unknown option: $1" ;;
    esac
    shift
  done
}

# stdin is the script itself when piped, so every prompt reads from /dev/tty.
have_tty() { (: </dev/tty) 2>/dev/null; }

confirm() {
  local answer=""
  printf '%s [y/N] ' "$1"
  read -r answer </dev/tty || answer=""
  case "$answer" in y|Y|yes|YES|Yes) return 0 ;; *) return 1 ;; esac
}

add_to_path() {
  case ":$PATH:" in *":$1:"*) ;; *) PATH="$1:$PATH" ;; esac
  export PATH
  hash -r
}

# ---------------------------------------------------------------- checks

check_platform() {
  local os arch
  os="$(uname -s)"
  if [ "$os" != "Darwin" ]; then
    miss "This is $os. Jarvis is macOS-only for now."
    exit 1
  fi
  ok "macOS"
  arch="$(uname -m)"
  if [ "$arch" != "arm64" ] && [ "$(sysctl -n hw.optional.arm64 2>/dev/null || true)" != "1" ]; then
    warn "Intel Mac detected. Jarvis is only tested on Apple Silicon; trying anyway."
  fi
}

check_clt() {
  # Only run clang++/git once the tools are installed: on a bare Mac their
  # /usr/bin shims would pop up Apple's installer dialog by themselves.
  if xcode-select -p >/dev/null 2>&1 \
    && clang++ --version >/dev/null 2>&1 \
    && git --version >/dev/null 2>&1; then
    ok "Xcode Command Line Tools"
  else
    miss "Xcode Command Line Tools missing"
    NEED_CLT=1
  fi
}

node_major() {
  local v
  v="$(node -v 2>/dev/null || true)"
  v="${v#v}"
  printf '%s' "${v%%.*}"
}

check_node() {
  local major
  major="$(node_major)"
  if [[ "$major" =~ ^[0-9]+$ ]] && [ "$major" -ge "$NODE_MIN_MAJOR" ] && command -v npm >/dev/null 2>&1; then
    ok "Node.js $(node -v)"
  elif [[ "$major" =~ ^[0-9]+$ ]]; then
    miss "Node.js $(node -v) found, but Node.js $NODE_MIN_MAJOR or newer is needed"
    NEED_NODE=1
  else
    miss "Node.js missing"
    NEED_NODE=1
  fi
}

check_uv() {
  if [ -x "$HOME/.local/bin/uv" ]; then add_to_path "$HOME/.local/bin"; fi
  if command -v uv >/dev/null 2>&1; then
    ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"
  else
    miss "uv missing"
    NEED_UV=1
  fi
}

# --------------------------------------------------------------- installs

install_clt() {
  say "Opening Apple's installer for the Command Line Tools."
  say "Click \"Install\" in the dialog that appears, then wait; this can take a few minutes."
  xcode-select --install >/dev/null 2>&1 || true
  local waited=0
  while ! xcode-select -p >/dev/null 2>&1; do
    if [ "$waited" -ge 1800 ]; then
      die "Gave up waiting for the Command Line Tools after 30 minutes. Finish that install, then run this again."
    fi
    say "${DIM}Waiting for the Command Line Tools install to finish...${RESET}"
    sleep 5
    waited=$((waited + 5))
  done
  if ! { clang++ --version >/dev/null 2>&1 && git --version >/dev/null 2>&1; }; then
    die "The Command Line Tools are installed but clang++ or git still fail. Try again in a new Terminal."
  fi
  ok "Xcode Command Line Tools installed"
}

install_node() {
  local sums name expected actual
  TMP_DIR="$(mktemp -d)"
  say "Looking up the latest Node.js $NODE_MIN_MAJOR installer..."
  sums="$(curl -fsSL "$NODE_DIST/SHASUMS256.txt")" || die "Could not reach nodejs.org."
  name="$(printf '%s\n' "$sums" | awk '$2 ~ /^node-v24\..*\.pkg$/ {print $2; exit}')"
  expected="$(printf '%s\n' "$sums" | awk '$2 ~ /^node-v24\..*\.pkg$/ {print $1; exit}')"
  [ -n "$name" ] && [ -n "$expected" ] || die "Could not find a Node.js $NODE_MIN_MAJOR .pkg in SHASUMS256.txt."

  say "Downloading $name..."
  curl -fSL --progress-bar -o "$TMP_DIR/$name" "$NODE_DIST/$name" || die "Download failed."
  actual="$(shasum -a 256 "$TMP_DIR/$name" | awk '{print $1}')"
  [ "$actual" = "$expected" ] || die "Checksum mismatch for $name; not installing it."
  ok "Checksum verified"

  say "Installing Node.js. macOS will ask for your Mac password (sudo)."
  sudo installer -pkg "$TMP_DIR/$name" -target / || die "The Node.js installer failed."
  # Put the new node ahead of an older one (nvm, Homebrew) for the rest of this run.
  PATH="/usr/local/bin:$PATH"; export PATH; hash -r
  [ "$(node_major)" -ge "$NODE_MIN_MAJOR" ] 2>/dev/null || die "Node.js $NODE_MIN_MAJOR is not on PATH after installing."
  ok "Node.js $(node -v) installed"
}

install_uv() {
  say "Installing uv with its official installer..."
  curl -LsSf https://astral.sh/uv/install.sh | sh || die "The uv installer failed."
  add_to_path "$HOME/.local/bin"
  command -v uv >/dev/null 2>&1 || die "uv is not on PATH after installing."
  ok "uv installed"
}

# ------------------------------------------------------------------ repo

is_jarvis_clone() {
  [ -d "$1/.git" ] || return 1
  git -C "$1" remote get-url origin 2>/dev/null \
    | grep -Eiq 'github\.com[:/]samsara0xgg/jarvis(\.git)?/?$'
}

get_repo() {
  if [ ! -e "$INSTALL_DIR" ] || { [ -d "$INSTALL_DIR" ] && [ -z "$(ls -A "$INSTALL_DIR")" ]; }; then
    say "Cloning Jarvis into $INSTALL_DIR..."
    git clone -q --depth 1 "$REPO_URL" "$INSTALL_DIR" || die "git clone failed."
  elif is_jarvis_clone "$INSTALL_DIR"; then
    say "Updating the existing Jarvis clone in $INSTALL_DIR..."
    git -C "$INSTALL_DIR" pull --ff-only \
      || warn "Could not update (local changes?). Continuing with what is there."
  else
    die "$INSTALL_DIR already exists and is not a Jarvis clone. Pass --dir <path> to use another place."
  fi
  ok "Jarvis is in $INSTALL_DIR"
}

install_npm_deps() {
  cd "$INSTALL_DIR/desktop/resonance"
  if [ -d node_modules ] && [ -f node_modules/.package-lock.json ] \
    && ! [ package-lock.json -nt node_modules/.package-lock.json ]; then
    ok "npm dependencies already installed"
  else
    say "Installing npm dependencies (a few minutes; Electron is a big download)..."
    npm ci || die "npm ci failed."
    ok "npm dependencies installed"
  fi
}

# ------------------------------------------------------------------ main

print_plan() {
  say ""
  say "${BOLD}This will install:${RESET}"
  if [ "$NEED_CLT" = 1 ];  then say "  - Xcode Command Line Tools (Apple's installer dialog opens)"; fi
  if [ "$NEED_NODE" = 1 ]; then say "  - Node.js $NODE_MIN_MAJOR (official .pkg from nodejs.org; asks for your Mac password)"; fi
  if [ "$NEED_UV" = 1 ];   then say "  - uv (official installer from astral.sh, into ~/.local/bin)"; fi
  say "  - Jarvis, from $REPO_URL, into $INSTALL_DIR"
  say "  - Its npm dependencies, inside that folder"
  say ""
}

print_next_steps() {
  say ""
  say "${BOLD}To run Jarvis for real${RESET} (daemon, voice, your own data), follow"
  say "\"Run it for real\" in the README: $REPO_URL#run-it-for-real"
  say "  cd $INSTALL_DIR && uv sync && uv run python -m jarvis serve"
  say ""
}

main() {
  setup_colors
  parse_args "$@"
  trap cleanup EXIT

  say "${BOLD}✦ Jarvis demo${RESET}"
  say ""
  check_platform
  if [ "$ASSUME_YES" != 1 ] && ! have_tty; then
    die "Cannot ask for confirmation (no terminal). Run this in Terminal, or add --yes: curl -fsSL https://raw.githubusercontent.com/samsara0xgg/Jarvis/main/scripts/try-demo.sh | bash -s -- --yes"
  fi
  check_clt
  check_node
  if [ "$FULL" = 1 ]; then check_uv; fi

  print_plan
  if [ "$ASSUME_YES" != 1 ] && ! confirm "Continue?"; then
    say "Nothing was installed."
    exit 0
  fi

  if [ "$NEED_CLT" = 1 ];  then install_clt; fi
  if [ "$NEED_NODE" = 1 ]; then install_node; fi
  if [ "$NEED_UV" = 1 ];   then install_uv; fi

  get_repo
  install_npm_deps

  if [ "$FULL" = 1 ]; then print_next_steps; fi
  say "Starting the demo. Press Ctrl+C here to quit."
  cleanup
  exec npm run companion -- --demo
}

main "$@"
