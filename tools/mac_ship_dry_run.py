"""Run the Mac shipper (ADR 0294) over this machine's real TimeSink and Claude Code files, offline.

    .venv/bin/python tools/mac_ship_dry_run.py [--images 20] [--no-sessions] [--no-timesink]
        [--scale 1.0] [--quality 1.0] [--keep]

The brain is the real intake code (:class:`jarvis.state.shipments.Shipments`) called in this
process over a temporary runtime root: no network, no token, nothing sent anywhere. TimeSink is
opened read-only. Prints, per source and kind, the entries shipped and the bytes, then the
pictures. A second pass follows to show that nothing is sent twice.
"""

# ruff: noqa: T201

from __future__ import annotations

import argparse
import shutil
import tempfile
import time
from pathlib import Path

from jarvis.state import imports
from jarvis.state.memory_db import open_memory_db
from jarvis.state.shipments import Shipments, ShipRefused
from jarvis.surface.mac_ship import ShipConfig, Shipper, ship_config
from jarvis.surface.mac_ship_core import Conflict, Report, ShipFailed, State
from jarvis.surface.mac_ship_timesink import TimesinkConfig


class LocalSink:
    """The brain's intake called directly."""

    def __init__(self, intake: Shipments) -> None:
        """Wrap the intake."""
        self.intake = intake

    def send_entries(self, source: str, body: bytes) -> None:
        """Keep one JSONL body."""
        try:
            self.intake.receive_entries(source, body)
        except ShipRefused as exc:
            raise ShipFailed(str(exc)) from None

    def send_file(self, source: str, relpath: str, data: bytes) -> str:
        """Keep one file; its SHA-256."""
        try:
            return self.intake.receive_file(source, relpath, data).sha256
        except ShipRefused as exc:
            raise (Conflict if exc.status == 409 else ShipFailed)(str(exc)) from None  # noqa: PLR2004


def _show(title: str, report: Report, seconds: float) -> None:
    print(f"--- {title} ({seconds:.1f} s)")
    for kind, count in sorted(report.entries.items()):
        print(f"  {kind:<40} {count:>8}")
    print(
        f"  entries total {sum(report.entries.values())}, {report.entry_bytes:,} bytes sent;"
        f" pictures {report.files}, {report.file_bytes:,} bytes"
        f" (originals {report.file_original_bytes:,})",
    )
    if report.skipped:
        print(f"  skipped {dict(report.skipped)}")
    if report.failed:
        print(f"  failed {report.failed}")


def main() -> None:
    """Run two passes and print what each did."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", type=int, default=20, help="pictures to ship (0: none)")
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--quality", type=float, default=1.0)
    parser.add_argument("--no-sessions", action="store_true")
    parser.add_argument("--no-timesink", action="store_true")
    parser.add_argument("--keep", action="store_true", help="keep the temporary root")
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="jarvis-ship-dry-"))
    try:
        base = ship_config({"mac_ship": {"enabled": True}}, root)
        assert base is not None  # noqa: S101 - enabled above
        timesink = base.timesink
        if timesink is not None:
            timesink = TimesinkConfig(
                timesink.db_path, images=args.images > 0, image_scale=args.scale,
                image_quality=args.quality, images_per_poll=max(1, args.images),
            )
        config = ShipConfig(
            base.state_path, base.poll_interval_s,
            None if args.no_timesink else timesink,
            () if args.no_sessions else base.session_roots, base.desktop_titles,
        )
        memory = root / "memory.db"
        intake = Shipments(root / imports.IMPORTS_DIRNAME, memory, min_free_bytes=0)
        shipper = Shipper(config, LocalSink(intake), State(config.state_path))
        for title in ("first pass", "second pass (nothing new expected)"):
            started = time.monotonic()
            report = shipper.poll()
            _show(title, report, time.monotonic() - started)
        conn = open_memory_db(memory)
        print("--- brain side")
        rows = conn.execute(
            "SELECT source, kind, count(*), sum(length(fields) + length(text)) "
            "FROM import_entries GROUP BY source, kind ORDER BY 1, 2",
        ).fetchall()
        for source, kind, count, size in rows:
            rows_now = len(imports.newest(conn, source, kinds=(kind,)))
            print(
                f"  {source}/{kind:<30} revisions {count:>8}  newest-per-row {rows_now:>8}"
                f"  {size:>12,} bytes",
            )
        archive = root / imports.IMPORTS_DIRNAME
        size = sum(f.stat().st_size for f in archive.rglob("*") if f.is_file())
        print(f"  imports archive {size:,} bytes; memory.db {memory.stat().st_size:,} bytes")
        print(f"  temporary root {root}{' (kept)' if args.keep else ''}")
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
