#!/usr/bin/env bash
# Build the dev environment a fresh Linux checkout gates in, then prove it.
#
# Exactly what uv.lock pins (--frozen: a drifting lock is a failure, not a
# re-resolve), the dev extra for the gate tools, Python 3.12 as .mise.toml and
# scripts/init.sh expect. The foundation check follows, so a CI run that goes red
# afterwards is code, not environment (the same promise init.sh makes locally).
# Used by .github/workflows/ci.yml; runs the same by hand on any Linux or Mac with uv.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

uv sync --frozen --extra dev --python 3.12
bash scripts/init.sh
