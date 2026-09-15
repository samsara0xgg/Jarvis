#!/usr/bin/env python3
"""Assert the docs/adr/README.md standard.

Enumerates both sides and prints the counts, then exits nonzero on a real
break. Legacy files, listed in PRE_STANDARD, are reported but never failed:
retrofitting fourteen pre-standard ADRs is a separate decision. What fails is
a file that claims the new format and breaks it, a duplicate number among such
files, or a NEW dangling ADR reference anywhere in the repository.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ADR_DIR = Path(__file__).resolve().parent.parent / "docs" / "adr"
STATUS_LINE = 2  # zero-based: the standard puts **Status:** on the third line
LINE_CAP = 200
STATUS_RE = re.compile(
    r"^\*\*Status:\*\* (Proposed|Accepted|Superseded-by-(\d{4})|Rejected|Frozen)$"
)
FILENAME_RE = re.compile(r"^(\d{4})-[a-z0-9]+(-[a-z0-9]+)*\.md$")
REFERENCE_RE = re.compile(r"\bADR[- ]?(\d{4})\b")
HEADING_RE = re.compile(r"^#{2,3} *[\d.]* *(.+)$", re.MULTILINE)
ALTERNATIVES_RE = re.compile(
    r"^## Alternatives rejected$(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL
)
REQUIRED = ("## Context", "## Decision", "## Alternatives rejected", "## Consequences")
FORBIDDEN = (
    "build order",
    "definition of done",
    "acceptance",
    "spec deviations",
    "module map",
    "wire protocol",
    "file-level change map",
    "amendments",
)
SCANNED_SUFFIXES = (".py", ".md", ".html", ".yaml", ".yml", ".swift", ".ts", ".tsx")

# ponytail: three numbers were cited by ADRs and production code but never
# written. Allowlisted so the check catches the NEXT dangling reference instead
# of drowning in these. Remove an entry when its ADR is written or its
# citations are deleted.
NEVER_WRITTEN = {"0004", "0007", "0010"}

# The fourteen ADRs that predate this standard. Every other file is checked, so
# a new ADR cannot skip the checks by accident. Remove a name when that file is
# frozen or rewritten; when the set is empty, delete it and this comment.
# An explicit set, not a heuristic: 0005 is the one legacy file whose Status
# line already happens to match the new enum, so any rule inferred from the
# header misclassifies it.
PRE_STANDARD = {
    "0001-legacy-scan.md",
    "0001-mac-only-flagship-scenario.md",
    "0002-real-codex-flagship-scenario.md",
    "0003-inherent-text.md",
    "0005-inherent-voice.md",
    "0006-full-duplex-voice-session.md",
    "0008-real-time-response-streaming.md",
    "0009-residency-and-perception.md",
    "0011-tool-surface-v1.md",
    "0012-confirmation-flow.md",
    "0014-inherent-realtime-ux.md",
    "0015-resonance-surface-residency.md",
    "0016-live-voice-provider-and-delegation-bridge.md",
    "0018-usage-observer-and-quota-dashboard.md",
}


def repo_root() -> Path:
    """Return the repository root that owns ADR_DIR."""
    return ADR_DIR.parent.parent


def scanned_files() -> list[Path]:
    """Return every tracked file whose suffix can carry an ADR citation."""
    root = repo_root()
    listing = subprocess.run(
        ["git", "ls-files", "-z"],  # noqa: S607 — git via PATH, same posture as tests/scenarios
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [root / p for p in listing.split("\0") if p.endswith(SCANNED_SUFFIXES)]


def check_file(path: Path, seen: dict[str, Path]) -> list[str]:
    """Return every standard break in one post-standard ADR."""
    breaks: list[str] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    status = STATUS_RE.match(lines[STATUS_LINE]) if len(lines) > STATUS_LINE else None
    if not status:
        return [f"{path.name}: line 3 is not a **Status:** line from the closed set"]

    name = FILENAME_RE.match(path.name)
    if not name:
        return [f"{path.name}: filename is not NNNN-kebab-case-title.md"]
    number = name.group(1)
    if number in seen:
        breaks.append(f"{path.name}: number {number} already used by {seen[number].name}")
    seen[number] = path

    if status.group(1) == "Frozen":
        return breaks

    body = "\n".join(lines)
    breaks.extend(
        f"{path.name}: missing required heading {heading!r}"
        for heading in REQUIRED
        if not re.search(rf"^{re.escape(heading)}$", body, re.MULTILINE)
    )
    breaks.extend(
        f"{path.name}: forbidden heading {heading.strip()!r} (see README rule 5)"
        for heading in HEADING_RE.findall(body)
        if heading.strip().lower().rstrip(":") in FORBIDDEN
    )
    alternatives = ALTERNATIVES_RE.search(body)
    if alternatives and not re.search(r"^ *[-*] ", alternatives.group(1), re.MULTILINE):
        breaks.append(f"{path.name}: 'Alternatives rejected' has no entry (README rule 4)")
    if len(lines) > LINE_CAP:
        breaks.append(f"{path.name}: {len(lines)} lines exceeds the {LINE_CAP}-line cap")

    superseded_by = status.group(2)
    if superseded_by and not list(ADR_DIR.glob(f"{superseded_by}-*.md")):
        breaks.append(f"{path.name}: Superseded-by-{superseded_by} names no existing ADR")
    return breaks


def dangling_references(existing: set[str]) -> list[str]:
    """Return one break per ADR number cited in the repository but never written."""
    sites: dict[str, list[str]] = {}
    root = repo_root()
    for path in scanned_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number in set(REFERENCE_RE.findall(text)):
            if number not in existing and number not in NEVER_WRITTEN:
                sites.setdefault(number, []).append(str(path.relative_to(root)))
    return [
        f"ADR-{number} is referenced but does not exist: {', '.join(sorted(where)[:4])}"
        for number, where in sorted(sites.items())
    ]


def main() -> int:
    """Print both counts, then report every break. Returns the exit status."""
    files = sorted(p for p in ADR_DIR.glob("*.md") if p.name != "README.md")
    legacy = [p.name for p in files if p.name in PRE_STANDARD]
    seen: dict[str, Path] = {}
    breaks = [b for p in files if p.name not in PRE_STANDARD for b in check_file(p, seen)]

    existing = {m.group(1) for p in files if (m := FILENAME_RE.match(p.name))}
    breaks += dangling_references(existing)

    out = sys.stdout
    out.write(f"ADR files: {len(files)}   conforming: {len(seen)}   legacy: {len(legacy)}\n")
    out.write(
        f"numbers present: {len(existing)}   "
        f"never written but cited: {len(NEVER_WRITTEN)}\n"
    )
    if legacy:
        out.write(f"legacy (pre-standard, not failed): {', '.join(legacy)}\n")
    for b in breaks:
        out.write(f"BREAK  {b}\n")
    out.write("OK\n" if not breaks else f"{len(breaks)} break(s)\n")
    return 1 if breaks else 0


if __name__ == "__main__":
    sys.exit(main())
