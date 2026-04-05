"""SQLite-backed memory store — memories, user profiles, and episodes."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

LOGGER = logging.getLogger(__name__)


def _serialize_embedding(vec: np.ndarray) -> bytes:
    """Serialize a numpy float32 vector to bytes."""
    return vec.astype(np.float32).tobytes()


def _deserialize_embedding(blob: bytes) -> np.ndarray:
    """Deserialize bytes back to a numpy float32 vector."""
    return np.frombuffer(blob, dtype=np.float32).copy()


class MemoryStore:
    """Persistent storage for memories, user profiles, and conversation episodes.

    Uses a single SQLite database with three tables.

    Args:
        db_path: Path to the SQLite database file.
    """

    def __init__(self, db_path: str | Path = "data/memory/jarvis_memory.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Get a thread-local SQLite connection (WAL mode for concurrent access)."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return self._local.conn

    def _init_db(self) -> None:
        """Create tables if they don't exist, and migrate schema if needed."""
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id          TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL,
                content     TEXT NOT NULL,
                category    TEXT NOT NULL,
                key         TEXT,
                importance  REAL DEFAULT 5.0,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                last_accessed TEXT,
                access_count INTEGER DEFAULT 0,
                source      TEXT DEFAULT 'extracted',
                time_ref    TEXT,
                expires     TEXT,
                tags        TEXT,
                superseded_by TEXT,
                active      INTEGER DEFAULT 1,
                embedding   BLOB
            );

            CREATE INDEX IF NOT EXISTS idx_memories_user_active
                ON memories(user_id, active);

            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id     TEXT PRIMARY KEY,
                profile     TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS episodes (
                id          TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL,
                session_id  TEXT NOT NULL,
                summary     TEXT NOT NULL,
                date        TEXT NOT NULL,
                mood        TEXT,
                topics      TEXT,
                created_at  TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_episodes_user_date
                ON episodes(user_id, date);

            CREATE TABLE IF NOT EXISTS episode_digests (
                id          TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL,
                period_start TEXT NOT NULL,
                period_end  TEXT NOT NULL,
                digest      TEXT NOT NULL,
                created_at  TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_digests_user_period
                ON episode_digests(user_id, period_end DESC);
        """)
        conn.commit()
        self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Add columns introduced after initial schema."""
        cols = {row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
        # v2: add 'key' column
        if "key" not in cols:
            conn.execute("ALTER TABLE memories ADD COLUMN key TEXT")
            LOGGER.info("Migrated memories table: added 'key' column")
        # v3: add 'expires' column
        if "expires" not in cols:
            conn.execute("ALTER TABLE memories ADD COLUMN expires TEXT")
            LOGGER.info("Migrated memories table: added 'expires' column")
        conn.commit()
        # Ensure key index exists (safe to re-run)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_user_key "
            "ON memories(user_id, category, key) WHERE key IS NOT NULL AND active = 1"
        )
        conn.commit()

    def close(self) -> None:
        """Close the current thread's database connection."""
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None

    # ------------------------------------------------------------------
    # Memories
    # ------------------------------------------------------------------

    def add_memory(
        self,
        user_id: str,
        content: str,
        category: str,
        key: str | None = None,
        importance: float = 5.0,
        tags: list[str] | None = None,
        time_ref: str | None = None,
        expires: str | None = None,
        source: str = "extracted",
        embedding: np.ndarray | None = None,
    ) -> str:
        """Insert a new memory entry.

        Args:
            key: Semantic key for fact/preference/knowledge (e.g. "location",
                "favorite_drink"). Used for deterministic dedup. Events have no key.
            expires: ISO date string after which this memory is considered stale
                (e.g. "2026-04-07"). Used for event/task types. None = never expires.

        Returns:
            The generated memory ID.
        """
        mem_id = str(uuid.uuid4())[:12]
        now = datetime.now().isoformat()
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO memories
               (id, user_id, content, category, key, importance, created_at,
                updated_at, last_accessed, access_count, source, time_ref,
                expires, tags, superseded_by, active, embedding)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, NULL, 1, ?)""",
            (
                mem_id, user_id, content, category, key, importance,
                now, now, now, source, time_ref, expires,
                json.dumps(tags or [], ensure_ascii=False),
                _serialize_embedding(embedding) if embedding is not None else None,
            ),
        )
        conn.commit()
        LOGGER.info("Memory added: %s [%s/%s] %s", mem_id, category, key or "-", content[:60])
        return mem_id

    def supersede_memory(self, old_id: str, new_id: str) -> None:
        """Mark an old memory as superseded by a new one."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE memories SET superseded_by = ?, active = 0, updated_at = ? WHERE id = ?",
            (new_id, datetime.now().isoformat(), old_id),
        )
        conn.commit()
        LOGGER.info("Memory superseded: %s → %s", old_id, new_id)

    def deactivate_memory_by_id(self, memory_id: str) -> bool:
        """Deactivate a specific memory by ID. Returns True if found."""
        conn = self._get_conn()
        cursor = conn.execute(
            "UPDATE memories SET active = 0, updated_at = ? WHERE id = ? AND active = 1",
            (datetime.now().isoformat(), memory_id),
        )
        conn.commit()
        return cursor.rowcount > 0

    def deactivate_memory(self, user_id: str, content_match: str) -> bool:
        """Deactivate a memory matching user_id and content substring.

        Returns:
            True if a memory was deactivated.
        """
        conn = self._get_conn()
        cursor = conn.execute(
            "UPDATE memories SET active = 0, updated_at = ? "
            "WHERE user_id = ? AND active = 1 AND content LIKE ?",
            (datetime.now().isoformat(), user_id, f"%{content_match}%"),
        )
        conn.commit()
        return cursor.rowcount > 0

    def get_active_memories(self, user_id: str) -> list[dict[str, Any]]:
        """Return all active memories for a user, with deserialized embeddings."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM memories WHERE user_id = ? AND active = 1 "
            "ORDER BY importance DESC, created_at DESC",
            (user_id,),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_memories_by_categories(
        self, user_id: str, categories: set[str],
    ) -> list[dict[str, Any]]:
        """Return active memories filtered by category, with deserialized embeddings."""
        if not categories:
            return []
        conn = self._get_conn()
        placeholders = ",".join("?" for _ in categories)
        rows = conn.execute(
            f"SELECT * FROM memories WHERE user_id = ? AND active = 1 "
            f"AND category IN ({placeholders}) "
            f"ORDER BY importance DESC, created_at DESC",
            (user_id, *categories),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def find_by_key(self, user_id: str, category: str, key: str) -> dict[str, Any] | None:
        """Find an active memory by category + key (deterministic dedup).

        Returns:
            The memory dict, or None if not found.
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM memories WHERE user_id = ? AND category = ? "
            "AND key = ? AND active = 1 LIMIT 1",
            (user_id, category, key),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def get_memory_summaries(self, user_id: str) -> list[str]:
        """Return content strings of all active memories (for dedup prompts)."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT content FROM memories WHERE user_id = ? AND active = 1",
            (user_id,),
        ).fetchall()
        return [r["content"] for r in rows]

    def touch_memory(self, memory_id: str) -> None:
        """Increment access_count and update last_accessed."""
        self.touch_many([memory_id])

    def touch_many(self, memory_ids: list[str]) -> None:
        """Batch increment access_count and update last_accessed."""
        if not memory_ids:
            return
        now = datetime.now().isoformat()
        conn = self._get_conn()
        conn.executemany(
            "UPDATE memories SET access_count = access_count + 1, "
            "last_accessed = ? WHERE id = ?",
            [(now, mid) for mid in memory_ids],
        )
        conn.commit()

    def count_active(self, user_id: str) -> int:
        """Count active memories for a user."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM memories WHERE user_id = ? AND active = 1",
            (user_id,),
        ).fetchone()
        return row["cnt"]

    def get_all_user_ids(self) -> list[str]:
        """Return all user IDs that have at least one active memory."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT DISTINCT user_id FROM memories WHERE active = 1"
        ).fetchall()
        return [r["user_id"] for r in rows]

    def get_embedding_index(self, user_id: str) -> tuple[list[str], list[str], list[str], "np.ndarray | None"]:
        """Return (ids, contents, categories, embeddings_matrix) for all active memories with embeddings.

        Optimized for batch similarity computation — avoids deserializing all fields.
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, content, category, embedding FROM memories "
            "WHERE user_id = ? AND active = 1 AND embedding IS NOT NULL",
            (user_id,),
        ).fetchall()
        if not rows:
            return [], [], [], None
        ids = [r["id"] for r in rows]
        contents = [r["content"] for r in rows]
        categories = [r["category"] for r in rows]
        embeddings = np.stack([_deserialize_embedding(r["embedding"]) for r in rows])
        return ids, contents, categories, embeddings

    # ------------------------------------------------------------------
    # User Profiles
    # ------------------------------------------------------------------

    def get_profile(self, user_id: str) -> dict[str, Any] | None:
        """Return the user profile JSON, or None if not set."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT profile FROM user_profiles WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["profile"])
        except (json.JSONDecodeError, TypeError):
            return None

    def set_profile(self, user_id: str, profile: dict[str, Any]) -> None:
        """Create or update a user profile."""
        conn = self._get_conn()
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO user_profiles (user_id, profile, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET profile = ?, updated_at = ?",
            (user_id, json.dumps(profile, ensure_ascii=False), now,
             json.dumps(profile, ensure_ascii=False), now),
        )
        conn.commit()

    # ------------------------------------------------------------------
    # Episodes
    # ------------------------------------------------------------------

    def add_episode(
        self,
        user_id: str,
        session_id: str,
        summary: str,
        date: str,
        mood: str | None = None,
        topics: list[str] | None = None,
    ) -> str | None:
        """Insert a conversation episode summary.

        Skips insertion if a substantially similar episode already exists
        for the same user on the same date (character-level Jaccard > 0.70).

        Returns:
            The generated episode ID, or None if skipped as duplicate.
        """
        conn = self._get_conn()

        # Dedup: check last episode for same user on same date
        last = conn.execute(
            "SELECT summary FROM episodes WHERE user_id = ? AND date = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id, date),
        ).fetchone()
        if last and self._episode_similar(last["summary"], summary):
            LOGGER.debug("Episode skipped (duplicate): %s", summary[:40])
            return None

        ep_id = str(uuid.uuid4())[:12]
        now = datetime.now().isoformat()
        conn.execute(
            """INSERT INTO episodes
               (id, user_id, session_id, summary, date, mood, topics, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ep_id, user_id, session_id, summary, date,
                mood,
                json.dumps(topics or [], ensure_ascii=False),
                now,
            ),
        )
        conn.commit()
        return ep_id

    def _episode_similar(self, a: str, b: str) -> bool:
        """Check if two episode summaries are substantially similar.

        Uses character-level Jaccard similarity (cheap, no embedding needed).
        """
        set_a = set(a)
        set_b = set(b)
        if not set_a or not set_b:
            return False
        jaccard = len(set_a & set_b) / len(set_a | set_b)
        return jaccard > 0.70

    def get_recent_episodes(self, user_id: str, days: int = 3) -> list[dict[str, Any]]:
        """Return episodes from the last N days, newest first."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM episodes WHERE user_id = ? "
            "AND date >= date('now', 'localtime', ?) ORDER BY date DESC, created_at DESC",
            (user_id, f"-{days} days"),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Expiry maintenance
    # ------------------------------------------------------------------

    def sweep_expired(self) -> int:
        """Deactivate expired event/task memories. Returns count affected."""
        conn = self._get_conn()
        cursor = conn.execute(
            "UPDATE memories SET active = 0 "
            "WHERE active = 1 AND expires IS NOT NULL "
            "AND expires < date('now', 'localtime') "
            "AND category IN ('event', 'task')"
        )
        conn.commit()
        return cursor.rowcount

    def backfill_expires(self) -> int:
        """Set expires = time_ref + 3 days for event/task without expires. Returns count."""
        conn = self._get_conn()
        cursor = conn.execute(
            "UPDATE memories SET expires = date(time_ref, '+3 days') "
            "WHERE active = 1 AND expires IS NULL AND time_ref IS NOT NULL "
            "AND category IN ('event', 'task')"
        )
        conn.commit()
        return cursor.rowcount

    # ------------------------------------------------------------------
    # Episode Digests
    # ------------------------------------------------------------------

    def add_digest(
        self,
        user_id: str,
        period_start: str,
        period_end: str,
        digest: str,
    ) -> str:
        """Store a weekly episode digest."""
        digest_id = str(uuid.uuid4())[:12]
        now = datetime.now().isoformat()
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO episode_digests
               (id, user_id, period_start, period_end, digest, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (digest_id, user_id, period_start, period_end, digest, now),
        )
        conn.commit()
        return digest_id

    def get_recent_digests(self, user_id: str, limit: int = 4) -> list[dict]:
        """Return recent episode digests, newest first."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM episode_digests WHERE user_id = ? "
            "ORDER BY period_end DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        """Convert a memories Row to a dict with deserialized fields."""
        d = dict(row)
        # Deserialize embedding
        if d.get("embedding"):
            d["embedding"] = _deserialize_embedding(d["embedding"])
        else:
            d["embedding"] = None
        # Deserialize tags
        if d.get("tags"):
            try:
                d["tags"] = json.loads(d["tags"])
            except (json.JSONDecodeError, TypeError):
                d["tags"] = []
        else:
            d["tags"] = []
        return d
