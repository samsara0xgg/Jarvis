# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "anthropic>=0.42",
#   "openai>=1.50",
#   "google-generativeai>=0.8",
#   "groq>=0.11",
#   "tiktoken>=0.8",
#   "tqdm>=4.66",
#   "pyyaml>=6.0",
#   "plotly>=5.18",
#   "pandas>=2.0",
# ]
# ///
"""Observer Bench — 中文对话 → structured observation 抽取能力对照.

Zero invasion of bench_llm_v3.py. Reuses ModelSpec / calc_cost / extract_cache_metrics
/ make_bust_prefix as pure helpers, rewrites tool-call version of provider callers here.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Awaitable, Callable
from uuid import uuid4

# Make bench_llm_v3 importable (same scripts/ dir)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench_llm_v3 as v3  # noqa: E402

LOGGER = logging.getLogger("observer_bench")

# ===== §1 CONSTANTS (filled in Task 2) =====
# ===== §2 DATACLASSES (Task 3) =====
# ===== §3 FIXTURE I/O (Task 4) =====
# ===== §4 PROMPT + TOOL BUILDERS (Task 5) =====
# ===== §5 PROVIDER CALLERS (Tasks 6-8) =====
# ===== §6 RETRY + ASSEMBLY (Task 9) =====
# ===== §7 WARMUP (Task 11) =====
# ===== §8 EVALUATOR (Task 10) =====
# ===== §9 FIXTURE GENERATOR (Task 12) =====
# ===== §10 OUTPUT (Task 13) =====
# ===== §11 CLI (Task 14) =====


def main() -> None:
    raise NotImplementedError("Built in Task 14")


if __name__ == "__main__":
    main()
