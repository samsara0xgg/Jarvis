#!/usr/bin/env python3
"""Register a role generation, verify its manifest, and acquire its lease.

Usage: bootstrap.py hub | bootstrap.py lane <id>
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _harness

VERIFY_RE = re.compile(r"^\s*([A-Za-z_]+):\s*(\S+)\s*#\s*verify:\s*(.+?)\s*$", re.MULTILINE)
FIELD_RE = re.compile(r"^(generation|predecessor):\s*(\S+)", re.MULTILINE)

ENV_STUB = """# Harness environment facts

Machine-specific facts every role must know: daemons (pid, port, runtime
root), which venv has which stack, gate commands, things never to touch.
One fact per line; add `verify: <command>` where a command can check it.
"""


def parse_role(argv: list[str]) -> tuple[str, str | None]:
    """Read the role and optional lane identifier from CLI arguments."""
    if len(argv) >= 1 and argv[0] == "hub":
        return "hub", None
    if len(argv) >= 2 and argv[0] == "lane":  # noqa: PLR2004 - role and lane ID
        return "lane", argv[1]
    print("usage: bootstrap.py hub | bootstrap.py lane <id>", file=sys.stderr)
    sys.exit(2)


def verify(text: str) -> list[str]:
    """Compare manifest facts with their operator-authored shell checks."""
    lines = []
    for m in VERIFY_RE.finditer(text):
        key, want, cmd = m.groups()
        try:
            got = subprocess.run(  # noqa: S602 - manifest checks are operator-authored shell commands
                cmd, shell=True, capture_output=True, text=True, timeout=30, check=False,
            ).stdout.strip()
        except subprocess.TimeoutExpired:
            got = "<timeout>"
        ok = got == want or got.startswith(want) or (want.startswith(got) and got)
        lines.append(
            f"  {'MATCH   ' if ok else 'MISMATCH'} {key}: "
            f"manifest={want} actual={got or '<empty>'}",
        )
    return lines or ["  (no verify lines in manifest)"]


def main() -> int:
    """Register this session, print its handoff context, and claim the lease."""
    role, lane = parse_role(sys.argv[1:])
    key = "hub" if role == "hub" else f"lane-{lane}"
    root = _harness.harness_dir()
    sid = _harness.session_id()

    manifest_path = root / "manifest" / f"{key}.yaml"
    manifest = manifest_path.read_text() if manifest_path.exists() else ""
    fields = dict(FIELD_RE.findall(manifest))
    lease_path = root / "lease" / key
    lease = lease_path.read_text().split() if lease_path.exists() else []
    lease_gen = int(lease[1]) if len(lease) > 1 and lease[1].isdigit() else 0
    generation = max(int(fields.get("generation", 0) or 0), lease_gen + 1, 1)
    predecessor = fields.get("predecessor")
    if predecessor in (None, "", "none", "null", "~"):  # writer misread the schema; the lease knows
        predecessor = lease[0] if lease else None

    env_path = root / "env.md"
    if not env_path.exists():
        env_path.write_text(ENV_STUB)

    _harness.write_json(
        root / "sessions" / f"{sid}.meta.json",
        {
            "session": sid,
            "role": role,
            "lane": lane,
            "generation": generation,
            "predecessor": predecessor,
            "started": _harness.now(),
            "cwd": str(Path.cwd()),
        },
    )

    print(f"role={key} generation={generation} session={sid} predecessor={predecessor or 'none'}")
    print(f"harness={root}")
    print(f"env: {env_path}  (read it in full)")
    if manifest:
        print(f"\n--- manifest {manifest_path.name} ---")
        print(manifest.rstrip())
        print("--- verify ---")
        print("\n".join(verify(manifest)))
    else:
        print("\nno manifest: this is generation 1 of this role, or the predecessor left none")
    if role == "hub":
        inbox = sorted(p.name for p in (root / "inbox").iterdir() if p.is_file())
        print(f"\ninbox: {', '.join(inbox) if inbox else 'empty'}")
        lanes = sorted(p.name for p in (root / "lease").iterdir() if p.name.startswith("lane-"))
        print(f"lane leases: {', '.join(lanes) if lanes else 'none'}")

    lease_path.write_text(f"{sid} {generation} {_harness.now()}\n")
    _harness.append_jsonl(
        root / "events.jsonl",
        {"ts": _harness.now(), "type": "rotation", "session": sid, "role": f"{key}-g{generation}",
         "summary": f"bootstrapped, predecessor={predecessor or 'none'}"},
    )
    print(f"\nlease {key} acquired by {sid} (generation {generation})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
