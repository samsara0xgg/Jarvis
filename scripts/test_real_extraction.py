#!/usr/bin/env python3
"""Test memory extraction quality with real LLM calls.

Requires OPENAI_API_KEY environment variable.
Validates function calling extraction + postprocess pipeline.

Usage:
    OPENAI_API_KEY=sk-... python scripts/test_real_extraction.py
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import time

# Allow imports from project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from memory.manager import MemoryManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger("test_real_extraction")

# ──────────────────────────────────────────────────────────────────────
# Test conversations
# ──────────────────────────────────────────────────────────────────────

TEST_CONVERSATIONS: list[dict] = [
    {
        "name": "自我介绍",
        "messages": [
            {"role": "user", "content": "我叫Allen，住在温哥华，在Shopify做软件工程师"},
            {"role": "assistant", "content": "你好Allen！温哥华是个好地方。"},
        ],
        "expected_categories": ["identity"],
        "expected_keys": ["name", "location", "occupation"],
        "min_memories": 2,
    },
    {
        "name": "偏好表达",
        "messages": [
            {"role": "user", "content": "我特别喜欢喝拿铁，不喜欢吃香菜"},
            {"role": "assistant", "content": "记住了，拿铁和不吃香菜。"},
        ],
        "expected_categories": ["preference"],
        "expected_keys": ["favorite_drink", "dislike_food"],
        "min_memories": 2,
    },
    {
        "name": "关系人物",
        "messages": [
            {"role": "user", "content": "我妹妹叫小美，她在上海读大学"},
            {"role": "assistant", "content": "小美在上海啊，什么专业？"},
        ],
        "expected_categories": ["relationship"],
        "expected_keys": ["sister"],
        "min_memories": 1,
    },
    {
        "name": "事件计划",
        "messages": [
            {"role": "user", "content": "下周一我要去深圳参加一个技术峰会"},
            {"role": "assistant", "content": "好的，祝你峰会顺利！"},
        ],
        "expected_categories": ["event"],
        "expected_keys": [],
        "min_memories": 1,
        "should_have_time_ref": True,
        "should_have_expires": True,
    },
    {
        "name": "待办任务",
        "messages": [
            {"role": "user", "content": "提醒我这周五之前交那个项目报告"},
            {"role": "assistant", "content": "收到，周五之前交报告。"},
        ],
        "expected_categories": ["task"],
        "min_memories": 1,
        "should_have_time_ref": True,
        "should_have_expires": True,
    },
    {
        "name": "知识教学",
        "messages": [
            {"role": "user", "content": "WiFi密码是home2024，记一下"},
            {"role": "assistant", "content": "好的，记住了。"},
        ],
        "expected_categories": ["knowledge"],
        "expected_keys": ["wifi_password"],
        "min_memories": 1,
    },
    {
        "name": "纠正更新",
        "messages": [
            {"role": "user", "content": "不对，我现在不喝拿铁了，改喝美式了"},
            {"role": "assistant", "content": "好的，从拿铁改成美式了。"},
        ],
        "should_have_corrections": True,
    },
    {
        "name": "闲聊（不应提取）",
        "messages": [
            {"role": "user", "content": "今天天气真好啊"},
            {"role": "assistant", "content": "是啊，温哥华难得的好天气。"},
        ],
        "max_memories": 0,
    },
    {
        "name": "设备操作（不应提取）",
        "messages": [
            {"role": "user", "content": "帮我把客厅的灯开一下"},
            {"role": "assistant", "content": "已经打开客厅的灯了。"},
        ],
        "max_memories": 0,
    },
    {
        "name": "混合对话",
        "messages": [
            {"role": "user", "content": "我最近在学机器学习，用的Python。对了，我对花生过敏，别让餐厅放花生。"},
            {"role": "assistant", "content": "好的，花生过敏记住了。学ML用Python很好。"},
        ],
        "expected_categories": ["knowledge", "preference"],
        "min_memories": 2,
    },
]

# ──────────────────────────────────────────────────────────────────────
# Extraction runner
# ──────────────────────────────────────────────────────────────────────

def run_extraction(mgr: MemoryManager, conv: dict) -> tuple[dict | None, list[dict]]:
    """Run _call_llm_extract + _postprocess_extraction on a single conversation.

    Returns (raw_extraction, post_processed_memories).
    """
    text = mgr._messages_to_text(conv["messages"])

    try:
        extraction = mgr._call_llm_extract(text, None, [], "Allen")
    except Exception as exc:
        LOGGER.error("LLM call failed for '%s': %s", conv["name"], exc)
        return None, []

    if extraction and extraction.get("memories"):
        if hasattr(mgr, "_postprocess_extraction"):
            memories = mgr._postprocess_extraction(extraction["memories"])
        else:
            memories = extraction["memories"]
    else:
        memories = []

    return extraction, memories


# ──────────────────────────────────────────────────────────────────────
# Validation
# ──────────────────────────────────────────────────────────────────────

def validate_extraction(conv: dict, extraction: dict | None, memories: list[dict]) -> dict:
    """Check extraction results against expectations. Returns check results."""
    checks: dict = {}

    # memory count
    if "min_memories" in conv:
        checks["min_memories"] = len(memories) >= conv["min_memories"]
    if "max_memories" in conv:
        checks["max_memories"] = len(memories) <= conv["max_memories"]

    # categories
    if "expected_categories" in conv:
        actual_cats = {m.get("category") for m in memories}
        checks["categories"] = any(c in actual_cats for c in conv["expected_categories"])

    # key filled for categories that require it
    key_required_cats = {"identity", "preference", "relationship", "knowledge"}
    missing_keys: list[str] = []
    for m in memories:
        if m.get("category") in key_required_cats and not m.get("key"):
            missing_keys.append(m.get("content", "")[:30])
    checks["all_keys_filled"] = len(missing_keys) == 0
    if missing_keys:
        checks["key_missing_items"] = missing_keys

    # time_ref for events/tasks
    if conv.get("should_have_time_ref"):
        has_time_ref = any(
            m.get("time_ref")
            for m in memories
            if m.get("category") in ("event", "task")
        )
        checks["time_ref"] = has_time_ref

    # expires for events/tasks (postprocess should backfill)
    if conv.get("should_have_expires"):
        has_expires = any(
            m.get("expires")
            for m in memories
            if m.get("category") in ("event", "task")
        )
        checks["expires"] = has_expires

    # corrections
    if conv.get("should_have_corrections"):
        checks["corrections"] = bool(extraction and extraction.get("corrections"))

    # episode summary
    checks["has_episode"] = bool(extraction and extraction.get("episode_summary"))

    return checks


# ──────────────────────────────────────────────────────────────────────
# Output formatting
# ──────────────────────────────────────────────────────────────────────

_PASS = "\033[92m\u2705\033[0m"
_FAIL = "\033[91m\u274c\033[0m"


def print_result(
    idx: int,
    total: int,
    conv: dict,
    extraction: dict | None,
    memories: list[dict],
    checks: dict,
) -> None:
    """Print formatted result for a single conversation."""
    name = conv["name"]
    n_mem = len(memories)
    print(f"\n[{idx}/{total}] {name}")
    print(f"  提取: {n_mem} 条记忆")

    for key, val in checks.items():
        if key == "key_missing_items":
            continue
        mark = _PASS if val else _FAIL
        detail = ""
        if key == "min_memories":
            threshold = conv.get("min_memories", "?")
            detail = f" ({n_mem} >= {threshold})" if val else f" ({n_mem} < {threshold})"
        elif key == "max_memories":
            threshold = conv.get("max_memories", "?")
            detail = f" ({n_mem} <= {threshold})" if val else f" ({n_mem} > {threshold})"
        elif key == "categories":
            actual = {m.get("category") for m in memories}
            detail = f" ({', '.join(sorted(actual))})"
        elif key == "all_keys_filled" and not val:
            items = checks.get("key_missing_items", [])
            detail = f" (missing: {items})"
        print(f"  {mark} {key}{detail}")

    # memory details
    if memories:
        print("  记忆详情:")
        for i, m in enumerate(memories, 1):
            cat = m.get("category", "?")
            key = m.get("key", "")
            content = m.get("content", "")[:50]
            imp = m.get("importance", "?")
            time_ref = m.get("time_ref", "")
            expires = m.get("expires", "")
            key_str = f"key={key}" if key else "key=MISSING"
            extra_parts: list[str] = []
            if time_ref:
                extra_parts.append(f"time_ref={time_ref}")
            if expires:
                extra_parts.append(f"expires={expires}")
            extra = (" | " + " | ".join(extra_parts)) if extra_parts else ""
            print(f"    {i}. [{cat}] {key_str} | {content} | imp={imp}{extra}")

    # corrections
    if extraction and extraction.get("corrections"):
        print("  纠正:")
        for c in extraction["corrections"]:
            print(f"    - {c.get('old_content', '?')} -> {c.get('new_content', '?')}")


def print_summary(all_results: list[dict], all_memories_flat: list[dict]) -> None:
    """Print aggregate summary."""
    print("\n" + "=" * 60)
    print("=== 总结 ===")

    # pass rate
    total_checks = 0
    passed_checks = 0
    for res in all_results:
        for k, v in res.items():
            if k == "key_missing_items":
                continue
            total_checks += 1
            if v:
                passed_checks += 1
    pct = (passed_checks / total_checks * 100) if total_checks else 0
    print(f"  检查通过率: {passed_checks}/{total_checks} ({pct:.0f}%)")

    # key fill rate
    key_cats = {"identity", "preference", "relationship", "knowledge"}
    need_key = [m for m in all_memories_flat if m.get("category") in key_cats]
    has_key = [m for m in need_key if m.get("key")]
    if need_key:
        print(f"  key 填充率: {len(has_key)}/{len(need_key)} ({len(has_key)/len(need_key)*100:.0f}%)")
    else:
        print("  key 填充率: N/A (no memories requiring key)")

    # time_ref fill rate
    time_cats = {"event", "task"}
    need_time = [m for m in all_memories_flat if m.get("category") in time_cats]
    has_time = [m for m in need_time if m.get("time_ref")]
    if need_time:
        print(f"  time_ref 填充率: {len(has_time)}/{len(need_time)} ({len(has_time)/len(need_time)*100:.0f}%)")

    # expires fill rate
    has_expires = [m for m in need_time if m.get("expires")]
    if need_time:
        print(f"  expires 填充率: {len(has_expires)}/{len(need_time)} ({len(has_expires)/len(need_time)*100:.0f}%)")

    # ignore correctness (chat / device)
    ignore_convs = [c for c in TEST_CONVERSATIONS if "max_memories" in c and c["max_memories"] == 0]
    ignore_correct = sum(
        1 for r in all_results
        if r.get("max_memories") is True
    )
    if ignore_convs:
        print(f"  闲聊/设备正确忽略: {ignore_correct}/{len(ignore_convs)}")

    # importance stats
    importances = [m.get("importance", 0) for m in all_memories_flat if m.get("importance")]
    if importances:
        avg_imp = sum(importances) / len(importances)
        print(f"  平均 importance: {avg_imp:.1f} (min={min(importances)}, max={max(importances)})")

    print("=" * 60)


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────

def main() -> None:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        print("错误: 请设置 OPENAI_API_KEY 环境变量")
        print("用法: OPENAI_API_KEY=sk-... python scripts/test_real_extraction.py")
        sys.exit(1)

    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    base_url = os.environ.get("OPENAI_BASE_URL", "")

    print("=" * 60)
    print("=== 记忆提取质量测试 ===")
    print(f"使用模型: {model}")
    if base_url:
        print(f"Base URL: {base_url}")
    print("=" * 60)

    all_results: list[dict] = []
    all_memories_flat: list[dict] = []
    total = len(TEST_CONVERSATIONS)

    for idx, conv in enumerate(TEST_CONVERSATIONS, 1):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_config: dict = {
                "memory": {"db_path": os.path.join(tmpdir, "test.db")},
                "llm": {
                    "api_key": api_key,
                    "model": model,
                    "base_url": base_url or "https://api.openai.com/v1",
                },
            }
            mgr = MemoryManager(test_config)

            t0 = time.time()
            extraction, memories = run_extraction(mgr, conv)
            elapsed = time.time() - t0

            checks = validate_extraction(conv, extraction, memories)
            all_results.append(checks)
            all_memories_flat.extend(memories)

            print_result(idx, total, conv, extraction, memories, checks)
            print(f"  耗时: {elapsed:.2f}s")

    print_summary(all_results, all_memories_flat)


if __name__ == "__main__":
    main()
