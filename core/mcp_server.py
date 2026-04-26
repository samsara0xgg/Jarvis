"""Read-only MCP server exposing jarvis trace queries.

Lets Claude Code (or any MCP client) ask natural-language questions about
jarvis trace history without writing SQL or starting jarvis.

Three tools, all read-only, all wrap existing ``memory/trace.py`` queries:

- ``trace_query_recent`` — recent rows with optional error/interrupted filters.
- ``trace_get_by_id`` — single row by primary key.
- ``trace_get_by_session_turn`` — single row by ``(session_id, turn_id)``.

Run as a stdio MCP server::

    python -m core.mcp_server [--db PATH]

DB path resolution: ``--db`` CLI override > ``config.yaml::memory.db_path``
> repo-relative ``data/memory/jarvis_memory.db``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

import yaml
from mcp.server.fastmcp import FastMCP

LOGGER = logging.getLogger(__name__)

_JSON_COLUMNS = (
    "input_metadata",
    "tool_calls",
    "llm_metadata",
    "latency_breakdown",
    "cited_obs_ids",
)

_DEFAULT_DB_RELATIVE = "data/memory/jarvis_memory.db"
_MAX_LIMIT = 200
_MAX_HOURS = 24 * 30  # 30 days


def _project_root() -> Path:
    """Repo root, derived from this file's location."""
    return Path(__file__).resolve().parent.parent


def resolve_db_path(cli_path: str | None = None) -> Path:
    """Resolve trace DB path: CLI override > config.yaml > repo default.

    Args:
        cli_path: Optional ``--db`` argument override.

    Returns:
        Absolute path to the trace SQLite file. Existence is not checked here.
    """
    if cli_path:
        return Path(cli_path).expanduser().resolve()

    config_file = _project_root() / "config.yaml"
    if config_file.exists():
        try:
            with config_file.open("r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            mem_db = cfg.get("memory", {}).get("db_path") if isinstance(cfg, dict) else None
            if isinstance(mem_db, str) and mem_db:
                p = Path(mem_db).expanduser()
                return p.resolve() if p.is_absolute() else (_project_root() / p).resolve()
        except (yaml.YAMLError, OSError) as exc:
            LOGGER.warning("Failed to read config.yaml; falling back to default: %s", exc)

    return (_project_root() / _DEFAULT_DB_RELATIVE).resolve()


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    """Open the trace DB in read-only mode (URI ``mode=ro``).

    Raises:
        FileNotFoundError: If ``db_path`` does not exist.
    """
    if not db_path.exists():
        raise FileNotFoundError(f"Trace DB not found: {db_path}")
    conn = sqlite3.connect(
        f"file:{db_path}?mode=ro",
        uri=True,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    return conn


def _deserialize_row(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a ``sqlite3.Row`` to a dict, deserializing JSON columns.

    Each JSON column is parsed independently. A malformed value yields
    ``{"_raw": ..., "_error": "JSONDecodeError"}`` rather than raising,
    so one bad row does not break a multi-row query.
    """
    d = dict(row)
    for col in _JSON_COLUMNS:
        raw = d.get(col)
        if raw is None or raw == "":
            d[col] = None
            continue
        try:
            d[col] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            LOGGER.warning(
                "Malformed JSON in column %s for trace_id=%s", col, d.get("id")
            )
            d[col] = {"_raw": raw, "_error": "JSONDecodeError"}
    return d


def build_server(db_path: Path) -> FastMCP:
    """Construct a FastMCP server bound to ``db_path``.

    Tools close their connection after each call so the DB file is not
    held open between requests; this keeps WAL footprint small and
    survives jarvis writers rotating the file.
    """
    mcp = FastMCP("jarvis-trace")

    @mcp.tool()
    def trace_query_recent(
        hours: int = 24,
        only_errors: bool = False,
        only_interrupted: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Recent jarvis traces newest-first.

        Args:
            hours: Look back this many hours from now (1..720).
            only_errors: Keep only rows where ``error`` is non-null.
            only_interrupted: Keep only rows where ``end_reason='interrupted'``.
            limit: Max rows returned (1..200).

        Returns:
            List of trace dicts with the 5 JSON columns deserialized.
        """
        if hours <= 0 or hours > _MAX_HOURS:
            raise ValueError(f"hours must be in (0, {_MAX_HOURS}]")
        bounded_limit = max(1, min(limit, _MAX_LIMIT))

        # julianday() parses both 'YYYY-MM-DDTHH:MM:SS.ffffff' (Python isoformat
        # with 'T' separator + microseconds, what the trace writer emits) and
        # SQLite's own 'YYYY-MM-DD HH:MM:SS' format, so the comparison is no
        # longer string-lex. 'localtime' modifier makes 'now' match the writer's
        # local-tz timestamps — without it, SQLite uses UTC and rows look like
        # they're in the future / past depending on offset.
        conditions = ["julianday(created_at) > julianday('now', ?, 'localtime')"]
        params: list[Any] = [f"-{hours} hours"]
        if only_errors:
            conditions.append("error IS NOT NULL")
        if only_interrupted:
            conditions.append("end_reason = 'interrupted'")
        sql = (
            f"SELECT * FROM trace WHERE {' AND '.join(conditions)} "
            "ORDER BY created_at DESC LIMIT ?"
        )
        params.append(bounded_limit)

        conn = _connect_ro(db_path)
        try:
            rows = conn.execute(sql, params).fetchall()
            return [_deserialize_row(r) for r in rows]
        finally:
            conn.close()

    @mcp.tool()
    def trace_get_by_id(trace_id: int) -> dict[str, Any] | None:
        """Fetch a single trace by primary key.

        Args:
            trace_id: ``trace.id`` (auto-increment primary key).

        Returns:
            Trace dict with JSON columns deserialized, or ``None`` if not found.
        """
        conn = _connect_ro(db_path)
        try:
            row = conn.execute(
                "SELECT * FROM trace WHERE id = ?", (trace_id,)
            ).fetchone()
            return _deserialize_row(row) if row else None
        finally:
            conn.close()

    @mcp.tool()
    def trace_get_by_session_turn(
        session_id: str, turn_id: int
    ) -> dict[str, Any] | None:
        """Fetch a trace by ``(session_id, turn_id)``.

        Args:
            session_id: Per-launch app session id (string).
            turn_id: Integer turn counter within the session.

        Returns:
            Trace dict with JSON columns deserialized, or ``None`` if not found.
        """
        conn = _connect_ro(db_path)
        try:
            row = conn.execute(
                "SELECT * FROM trace WHERE session_id = ? AND turn_id = ?",
                (session_id, turn_id),
            ).fetchone()
            return _deserialize_row(row) if row else None
        finally:
            conn.close()

    return mcp


def main() -> None:
    """Entry point for ``python -m core.mcp_server``."""
    parser = argparse.ArgumentParser(prog="jarvis-trace-mcp")
    parser.add_argument(
        "--db",
        default=None,
        help="Override trace DB path (otherwise config.yaml > default).",
    )
    args = parser.parse_args()

    db_path = resolve_db_path(args.db)
    log_level = os.environ.get("JARVIS_MCP_LOG", "WARNING").upper()
    logging.basicConfig(level=log_level)
    LOGGER.info("jarvis-trace MCP server starting; db=%s", db_path)

    server = build_server(db_path)
    server.run()


if __name__ == "__main__":
    main()
