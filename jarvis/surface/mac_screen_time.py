"""ADR 0304: iPhone and iPad app use, read from the Mac's synced Biome App.InFocus streams."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any, Final

from jarvis.surface.mac_location import ensure_app_bundle

_DIR: Final = Path(__file__).resolve().parents[2] / "native" / "screen_time"
_APP_NAME: Final = "JarvisScreenTime.app"
_IDENTIFIER: Final = "com.allen.jarvis.screen-time"
# Reading four weeks of segment files takes well under a second; this is the backstop.
_READ_TIMEOUT_S: Final = 60.0
# An "in" with no matching "out" is cut at the next event or this long, whichever is first.
_UNPAIRED_CAP_MS: Final = 30 * 60 * 1000
_TOP_APPS: Final = 15
_SHOWN: Final = 8


class ScreenTimeUnavailable(RuntimeError):  # noqa: N818 - reads as the reason it is raised for
    """The synced device usage could not be read; the message is the short reason."""


def _signer() -> str:
    """This Mac's first Apple Development identity, else "-" (ad-hoc).

    Signed by a certificate, the app keeps one designated requirement across rebuilds, so Full
    Disk Access is granted once; ad-hoc, every rebuild needs the grant again.
    """
    found = subprocess.run(
        ["/usr/bin/security", "find-identity", "-v", "-p", "codesigning"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout
    names = [line.split('"')[1] for line in found.splitlines() if '"Apple Development:' in line]
    return names[0] if names else "-"


def ensure_screen_time_app(source_dir: Path = _DIR) -> Path:
    """The Screen Time helper .app, built when missing or stale."""
    return ensure_app_bundle(
        source_dir, _APP_NAME, "jarvis-screen-time", _IDENTIFIER, signer=_signer()
    )


def parse_helper_output(text: str, app: Path | None = None) -> dict[str, Any]:
    """The helper's JSON object, or ``ScreenTimeUnavailable`` with the reason."""
    try:
        data = json.loads(text)
        error = data.get("error")
        if not error:
            devices, events = data["devices"], data["events"]
    except (ValueError, KeyError, AttributeError) as exc:
        msg = "the screen time helper gave no usable answer"
        raise ScreenTimeUnavailable(msg) from exc
    if error == "no_access":
        msg = f"no_access: grant Full Disk Access to {app or _APP_NAME}"
        raise ScreenTimeUnavailable(msg)
    if error:
        msg = str(error)
        raise ScreenTimeUnavailable(msg)
    answer: dict[str, Any] = {"devices": list(devices), "events": list(events)}
    if isinstance(data.get("streams"), dict):
        answer["streams"] = data["streams"]
    return answer


def read_screen_time(since_ms: int | None = None, streams: str | None = None) -> dict[str, Any]:
    """``{"devices": [...], "events": [...]}`` for the synced iPhone/iPad, at or after ``since_ms``.

    Each event is ``{"device", "bundle_id", "focus": "in"|"out", "at_ms"}`` (unix milliseconds).
    ``streams`` (comma separated: ``now_playing``, ``bluetooth``, ``interactions``) adds a
    ``"streams"`` object of those streams' records (ADR 0305, :mod:`mac_ship_records`).
    """
    if sys.platform != "darwin":
        msg = "this machine has no synced device usage"
        raise ScreenTimeUnavailable(msg)
    try:
        app = ensure_screen_time_app()
        with tempfile.TemporaryDirectory(prefix="jarvis-screen-time-") as tmp:
            out = Path(tmp) / "usage.json"
            argv = ["/usr/bin/open", "-W", "-n", str(app), "--args", str(out)]
            if since_ms is not None:
                argv += ["--since-ms", str(int(since_ms))]
            if streams:
                argv += ["--streams", streams]
            subprocess.run(  # noqa: S603 - fixed argv, repo-owned app, our own temp file
                argv,
                check=False,  # the answer is the file; the helper's exit code adds nothing
                capture_output=True,
                timeout=_READ_TIMEOUT_S,
            )
            # A helper that finishes at once can make `open -W` return before the file lands
            # ("Unable to block on application"); the helper writes it atomically, so poll.
            deadline = time.monotonic() + _READ_TIMEOUT_S
            while not out.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            text = out.read_text() if out.exists() else ""
    except subprocess.TimeoutExpired as exc:
        msg = "reading the device usage timed out"
        raise ScreenTimeUnavailable(msg) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        msg = f"the screen time helper failed ({type(exc).__name__})"
        raise ScreenTimeUnavailable(msg) from exc
    return parse_helper_output(text, app)


def closed_intervals(
    events: list[dict[str, Any]],
) -> tuple[list[tuple[str, int, int, bool]], tuple[str, int] | None]:
    """``(intervals, tail)``: the intervals that ended, and the last "in" nothing has followed.

    That ``(bundle_id, start_ms)`` may only be waiting for its "out" to sync, so it is not an
    interval yet; :func:`pair_intervals` cuts it at 30 minutes instead.
    """
    out: list[tuple[str, int, int, bool]] = []
    open_: tuple[str, int] | None = None
    for ev in sorted(events, key=lambda e: e["at_ms"]):
        at = int(ev["at_ms"])
        if open_ is not None:
            bundle, start = open_
            if ev["focus"] == "out" and ev["bundle_id"] == bundle:
                out.append((bundle, start, at, True))
                open_ = None
                continue
            if ev["focus"] == "in" and ev["bundle_id"] == bundle:
                continue  # repeated "in": keep the original start
            out.append((bundle, start, min(at, start + _UNPAIRED_CAP_MS), False))
            open_ = None
        if ev["focus"] == "in":
            open_ = (ev["bundle_id"], at)
    return out, open_


def pair_intervals(events: list[dict[str, Any]]) -> list[tuple[str, int, int, bool]]:
    """One device's events as ``(bundle_id, start_ms, end_ms, paired)``, oldest first.

    An "in" closed by its own "out" is paired. Otherwise (the next event is another app, or none
    follows) it is cut at that next event or 30 minutes and ``paired`` is False.
    """
    out, tail = closed_intervals(events)
    if tail is not None:
        out.append((tail[0], tail[1], tail[1] + _UNPAIRED_CAP_MS, False))
    return out


def summarize(data: dict[str, Any], tz: tzinfo | None = None) -> str:
    """Per device: event count, first and last time, then top apps by focus minutes per local day.

    A ``*`` after a minutes figure means some of it came from unpaired intervals (capped).
    """
    names = {d["id"]: d.get("name") or d["id"][:8] for d in data["devices"]}
    by_device: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ev in data["events"]:
        by_device[ev["device"]].append(ev)

    def local(ms: int) -> datetime:
        return datetime.fromtimestamp(ms / 1000, tz)

    lines: list[str] = []
    for device in sorted({*names, *by_device}, key=lambda d: names.get(d, d)):
        evs = by_device.get(device, [])
        label = names.get(device, device[:8])
        if not evs:
            lines.append(f"{label}: 0 events")
            continue
        first, last = (local(min(e["at_ms"] for e in evs)), local(max(e["at_ms"] for e in evs)))
        lines.append(f"{label}: {len(evs)} events, {first:%Y-%m-%d %H:%M} -> {last:%Y-%m-%d %H:%M}")
        per_day: dict[str, dict[str, list[float]]] = defaultdict(
            lambda: defaultdict(lambda: [0, 0])
        )
        for bundle, start, end, paired in pair_intervals(evs):
            cell = per_day[f"{local(start):%Y-%m-%d}"][bundle]
            cell[0] += (end - start) / 60000
            cell[1] += 0 if paired else (end - start) / 60000
        for day in sorted(per_day):
            apps = per_day[day]
            total = sum(c[0] for c in apps.values())
            lines.append(f"  {day}  total {total:.0f} min")
            top = sorted(apps.items(), key=lambda kv: -kv[1][0])[:_TOP_APPS]
            lines += [f"    {b:<48} {m:6.1f}{'*' if capped else ''}" for b, (m, capped) in top]
    return "\n".join(lines) if lines else "no synced devices"


def _print_entries(hours: float) -> int:
    """Counts per device and the first entries of :func:`mac_ship_devices.dry_run`."""
    from jarvis.surface import mac_ship_devices  # noqa: PLC0415 - it imports this module

    try:
        built, data, names = mac_ship_devices.dry_run(hours, int(time.time() * 1000))
    except ScreenTimeUnavailable as exc:
        sys.stderr.write(f"screen time unavailable: {exc}\n")
        return 1
    counts: dict[str, int] = defaultdict(int)
    for entry in built.entries:
        counts[f"{entry['fields']['device']} {entry['kind']}"] += 1
    bundles = {e["fields"]["bundle_id"] for e in built.entries if e["kind"] == "device_app"}
    sys.stdout.write(f"{len(data['events'])} events read, {len(built.entries)} entries\n")
    sys.stdout.writelines(f"  {name}: {n}\n" for name, n in sorted(counts.items()))
    sys.stdout.write(f"  app names: {len(bundles & names.keys())} of {len(bundles)} bundles\n")
    for entry in built.entries[:_SHOWN]:
        shown = {**entry["fields"], "app": names.get(entry["fields"].get("bundle_id"), (None,))[0]}
        sys.stdout.write(json.dumps(shown, ensure_ascii=False) + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    """``probe`` or ``entries --dry-run``, each over the last ``--hours`` (default 48)."""
    parser = argparse.ArgumentParser(prog="python -m jarvis.surface.mac_screen_time")
    sub = parser.add_subparsers(dest="cmd", required=True)
    probe = sub.add_parser("probe", help="summarise synced iPhone/iPad app use")
    probe.add_argument("--hours", type=float, default=48.0)
    entries = sub.add_parser("entries", help="what the shipper would send, without sending it")
    entries.add_argument("--hours", type=float, default=48.0)
    entries.add_argument("--dry-run", action="store_true", required=True)
    args = parser.parse_args(argv)
    if args.cmd == "entries":
        return _print_entries(args.hours)
    since_ms = int((time.time() - args.hours * 3600) * 1000)
    try:
        data = read_screen_time(since_ms)
    except ScreenTimeUnavailable as exc:
        sys.stderr.write(f"screen time unavailable: {exc}\n")
        return 1
    sys.stdout.write(summarize(data, datetime.now().astimezone().tzinfo) + "\n")
    return 0


__all__ = [
    "ScreenTimeUnavailable",
    "closed_intervals",
    "ensure_screen_time_app",
    "pair_intervals",
    "parse_helper_output",
    "read_screen_time",
    "summarize",
]


if __name__ == "__main__":
    sys.exit(main())
