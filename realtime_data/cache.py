"""文件缓存 + TTL."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)


class Cache:
    """文件缓存."""

    def __init__(self, cache_dir: str | Path) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def get(self, key: str, ttl_seconds: int) -> tuple[Any | None, bool]:
        """获取缓存，返回 (data, is_stale)."""
        cache_file = self.cache_dir / f"{key}.json"

        if not cache_file.exists():
            return None, False

        try:
            with open(cache_file) as f:
                cached = json.load(f)

            cached_at = datetime.fromisoformat(cached["cached_at"])
            age = (datetime.now() - cached_at).total_seconds()
            is_stale = age > ttl_seconds

            LOGGER.info(f"Cache {key}: age={age:.0f}s, stale={is_stale}")
            return cached["data"], is_stale

        except Exception as e:
            LOGGER.warning(f"Cache read failed for {key}: {e}")
            return None, False

    def set(self, key: str, data: Any) -> None:
        """写入缓存."""
        cache_file = self.cache_dir / f"{key}.json"

        try:
            with open(cache_file, "w") as f:
                json.dump({
                    "cached_at": datetime.now().isoformat(),
                    "data": data,
                }, f, indent=2)
            LOGGER.info(f"Cache {key}: written")
        except Exception as e:
            LOGGER.error(f"Cache write failed for {key}: {e}")
