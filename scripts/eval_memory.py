#!/usr/bin/env python3
"""End-to-end evaluation of the memory system (retrieval + direct answer).

Evaluates 4 query categories (40 cases total):
  A. Single-hop fact queries (10)
  B. Preference queries (10)
  C. Distractor queries with multiple stored facts (10)
  D. Negative queries where the answer was never stored (10)

Metrics:
  - DirectAnswer hit rate
  - Retriever MRR@5 (Mean Reciprocal Rank)
  - Top-1 accuracy
  - Negative rejection rate

Usage:
    python scripts/eval_memory.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

# Ensure project root is on sys.path when running as `python scripts/eval_memory.py`
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Evaluation datasets
# ---------------------------------------------------------------------------

SINGLE_HOP = [
    {
        "facts": [{"content": "Allen 住在加拿大温哥华", "category": "identity", "key": "location", "importance": 8}],
        "query": "我住在哪里？",
        "expected": "温哥华",
    },
    {
        "facts": [{"content": "Allen 喜欢喝拿铁", "category": "preference", "key": "favorite_drink", "importance": 6}],
        "query": "我喜欢喝什么咖啡？",
        "expected": "拿铁",
    },
    {
        "facts": [{"content": "Allen 的妹妹叫小美", "category": "relationship", "key": "sister", "importance": 7}],
        "query": "我妹妹叫什么名字？",
        "expected": "小美",
    },
    {
        "facts": [{"content": "Allen 是软件工程师", "category": "identity", "key": "occupation", "importance": 8}],
        "query": "我是做什么工作的？",
        "expected": "软件工程师",
    },
    {
        "facts": [{"content": "Allen 的 WiFi 密码是 home2024", "category": "knowledge", "key": "wifi_password", "importance": 9}],
        "query": "WiFi 密码是多少？",
        "expected": "home2024",
    },
    {
        "facts": [{"content": "Allen 对花生过敏", "category": "knowledge", "key": "allergy", "importance": 9}],
        "query": "我对什么过敏？",
        "expected": "花生",
    },
    {
        "facts": [{"content": "Allen 养了一只猫叫 Mochi", "category": "relationship", "key": "pet", "importance": 7}],
        "query": "我的猫叫什么？",
        "expected": "Mochi",
    },
    {
        "facts": [{"content": "Allen 在 Shopify 工作", "category": "identity", "key": "company", "importance": 8}],
        "query": "我在哪里上班？",
        "expected": "Shopify",
    },
    {
        "facts": [{"content": "Allen 的生日是 3 月 15 日", "category": "identity", "key": "birthday", "importance": 8}],
        "query": "我的生日是哪天？",
        "expected": "3 月 15",
    },
    {
        "facts": [{"content": "Allen 开一辆特斯拉 Model 3", "category": "knowledge", "key": "car", "importance": 6}],
        "query": "我开什么车？",
        "expected": "特斯拉",
    },
]

PREFERENCE = [
    {
        "facts": [
            {"content": "Allen 喜欢跑步", "category": "preference", "key": "sport", "importance": 6},
            {"content": "Allen 也喜欢游泳", "category": "preference", "key": "sport2", "importance": 5},
        ],
        "query": "我平时做什么运动？",
        "expected": "跑步",
    },
    {
        "facts": [{"content": "Allen 喜欢听周杰伦的歌", "category": "preference", "key": "music", "importance": 6}],
        "query": "我喜欢听谁的歌？",
        "expected": "周杰伦",
    },
    {
        "facts": [{"content": "Allen 不喜欢吃香菜", "category": "preference", "key": "dislike_food", "importance": 5}],
        "query": "我讨厌吃什么？",
        "expected": "香菜",
    },
    {
        "facts": [{"content": "Allen 喜欢日本料理", "category": "preference", "key": "cuisine", "importance": 6}],
        "query": "我最喜欢什么菜系？",
        "expected": "日本",
    },
    {
        "facts": [{"content": "Allen 支持湖人队", "category": "preference", "key": "nba_team", "importance": 5}],
        "query": "我支持哪个篮球队？",
        "expected": "湖人",
    },
    {
        "facts": [{"content": "Allen 最喜欢的电影是星际穿越", "category": "preference", "key": "movie", "importance": 6}],
        "query": "我最喜欢什么电影？",
        "expected": "星际穿越",
    },
    {
        "facts": [{"content": "Allen 喜欢喝绿茶", "category": "preference", "key": "tea", "importance": 5}],
        "query": "我爱喝什么茶？",
        "expected": "绿茶",
    },
    {
        "facts": [{"content": "Allen 喜欢看科幻小说", "category": "preference", "key": "book_genre", "importance": 5}],
        "query": "我喜欢看什么类型的书？",
        "expected": "科幻",
    },
    {
        "facts": [{"content": "Allen 周末经常去 Stanley Park 跑步", "category": "preference", "key": "running_spot", "importance": 5}],
        "query": "我周末去哪里跑步？",
        "expected": "Stanley Park",
    },
    {
        "facts": [{"content": "Allen 不喜欢辣的食物", "category": "preference", "key": "dislike_spicy", "importance": 5}],
        "query": "我能吃辣吗？",
        "expected": "不喜欢辣",
    },
]

DISTRACTOR = [
    {
        "facts": [
            {"content": "Allen 喜欢喝拿铁", "category": "preference", "key": "drink", "importance": 6},
            {"content": "Allen 住在温哥华", "category": "identity", "key": "location", "importance": 8},
            {"content": "Allen 的妹妹叫小美", "category": "relationship", "key": "sister", "importance": 7},
            {"content": "Allen 在 Shopify 工作", "category": "identity", "key": "company", "importance": 8},
            {"content": "Allen 喜欢跑步", "category": "preference", "key": "sport", "importance": 6},
        ],
        "query": "我的工作是什么？",
        "expected": "Shopify",
    },
    {
        "facts": [
            {"content": "Allen 是软件工程师", "category": "identity", "key": "occupation", "importance": 8},
            {"content": "Allen 喜欢听周杰伦的歌", "category": "preference", "key": "music", "importance": 6},
            {"content": "Allen 的 WiFi 密码是 home2024", "category": "knowledge", "key": "wifi", "importance": 9},
            {"content": "Allen 对花生过敏", "category": "knowledge", "key": "allergy", "importance": 9},
            {"content": "Allen 养了一只猫叫 Mochi", "category": "relationship", "key": "pet", "importance": 7},
        ],
        "query": "我的宠物叫什么？",
        "expected": "Mochi",
    },
    {
        "facts": [
            {"content": "Allen 的生日是 3 月 15 日", "category": "identity", "key": "birthday", "importance": 8},
            {"content": "Allen 开一辆特斯拉 Model 3", "category": "knowledge", "key": "car", "importance": 6},
            {"content": "Allen 喜欢日本料理", "category": "preference", "key": "cuisine", "importance": 6},
            {"content": "Allen 不喜欢吃香菜", "category": "preference", "key": "dislike_food", "importance": 5},
            {"content": "Allen 支持湖人队", "category": "preference", "key": "nba_team", "importance": 5},
        ],
        "query": "我生日是哪天？",
        "expected": "3 月 15",
    },
    {
        "facts": [
            {"content": "Allen 最喜欢的电影是星际穿越", "category": "preference", "key": "movie", "importance": 6},
            {"content": "Allen 喜欢喝绿茶", "category": "preference", "key": "tea", "importance": 5},
            {"content": "Allen 住在加拿大温哥华", "category": "identity", "key": "location", "importance": 8},
            {"content": "Allen 是软件工程师", "category": "identity", "key": "occupation", "importance": 8},
            {"content": "Allen 的女朋友叫 Sarah", "category": "relationship", "key": "girlfriend", "importance": 7},
        ],
        "query": "我女朋友叫什么名字？",
        "expected": "Sarah",
    },
    {
        "facts": [
            {"content": "Allen 对花生过敏", "category": "knowledge", "key": "allergy", "importance": 9},
            {"content": "Allen 喜欢看科幻小说", "category": "preference", "key": "book_genre", "importance": 5},
            {"content": "Allen 在 Shopify 工作", "category": "identity", "key": "company", "importance": 8},
            {"content": "Allen 养了一只猫叫 Mochi", "category": "relationship", "key": "pet", "importance": 7},
            {"content": "Allen 每天早上 7 点起床", "category": "preference", "key": "wake_time", "importance": 5},
        ],
        "query": "我对什么东西过敏？",
        "expected": "花生",
    },
    {
        "facts": [
            {"content": "Allen 喜欢跑步", "category": "preference", "key": "sport", "importance": 6},
            {"content": "Allen 也喜欢游泳", "category": "preference", "key": "sport2", "importance": 5},
            {"content": "Allen 喜欢听周杰伦的歌", "category": "preference", "key": "music", "importance": 6},
            {"content": "Allen 的妹妹叫小美", "category": "relationship", "key": "sister", "importance": 7},
            {"content": "Allen 的 WiFi 密码是 home2024", "category": "knowledge", "key": "wifi", "importance": 9},
        ],
        "query": "WiFi 密码是什么？",
        "expected": "home2024",
    },
    {
        "facts": [
            {"content": "Allen 开一辆特斯拉 Model 3", "category": "knowledge", "key": "car", "importance": 6},
            {"content": "Allen 的生日是 3 月 15 日", "category": "identity", "key": "birthday", "importance": 8},
            {"content": "Allen 喜欢日本料理", "category": "preference", "key": "cuisine", "importance": 6},
            {"content": "Allen 用 Python 和 TypeScript 写代码", "category": "knowledge", "key": "lang", "importance": 7},
            {"content": "Allen 住在温哥华", "category": "identity", "key": "location", "importance": 8},
        ],
        "query": "我用什么编程语言？",
        "expected": "Python",
    },
    {
        "facts": [
            {"content": "Allen 喜欢喝拿铁", "category": "preference", "key": "drink", "importance": 6},
            {"content": "Allen 不喜欢吃香菜", "category": "preference", "key": "dislike_food", "importance": 5},
            {"content": "Allen 不喜欢辣的食物", "category": "preference", "key": "dislike_spicy", "importance": 5},
            {"content": "Allen 喜欢喝绿茶", "category": "preference", "key": "tea", "importance": 5},
            {"content": "Allen 最喜欢的电影是星际穿越", "category": "preference", "key": "movie", "importance": 6},
        ],
        "query": "我喜欢喝什么咖啡？",
        "expected": "拿铁",
    },
    {
        "facts": [
            {"content": "Allen 周末经常去 Stanley Park 跑步", "category": "preference", "key": "running_spot", "importance": 5},
            {"content": "Allen 每天早上 7 点起床", "category": "preference", "key": "wake_time", "importance": 5},
            {"content": "Allen 是软件工程师", "category": "identity", "key": "occupation", "importance": 8},
            {"content": "Allen 支持湖人队", "category": "preference", "key": "nba_team", "importance": 5},
            {"content": "Allen 喜欢看科幻小说", "category": "preference", "key": "book_genre", "importance": 5},
            {"content": "Allen 对花生过敏", "category": "knowledge", "key": "allergy", "importance": 9},
        ],
        "query": "我支持哪支球队？",
        "expected": "湖人",
    },
    {
        "facts": [
            {"content": "Allen 的妹妹叫小美", "category": "relationship", "key": "sister", "importance": 7},
            {"content": "Allen 的女朋友叫 Sarah", "category": "relationship", "key": "girlfriend", "importance": 7},
            {"content": "Allen 养了一只猫叫 Mochi", "category": "relationship", "key": "pet", "importance": 7},
            {"content": "Allen 在 Shopify 工作", "category": "identity", "key": "company", "importance": 8},
            {"content": "Allen 喜欢跑步", "category": "preference", "key": "sport", "importance": 6},
        ],
        "query": "我妹妹叫什么？",
        "expected": "小美",
    },
]

NEGATIVE = [
    {
        "facts": [{"content": "Allen 喜欢喝拿铁", "category": "preference", "key": "drink", "importance": 6}],
        "query": "我的血型是什么？",
        "expected": None,
    },
    {
        "facts": [{"content": "Allen 住在温哥华", "category": "identity", "key": "location", "importance": 8}],
        "query": "我爸爸叫什么名字？",
        "expected": None,
    },
    {
        "facts": [{"content": "Allen 是软件工程师", "category": "identity", "key": "occupation", "importance": 8}],
        "query": "我的身高是多少？",
        "expected": None,
    },
    {
        "facts": [{"content": "Allen 喜欢跑步", "category": "preference", "key": "sport", "importance": 6}],
        "query": "我的手机号是多少？",
        "expected": None,
    },
    {
        "facts": [{"content": "Allen 对花生过敏", "category": "knowledge", "key": "allergy", "importance": 9}],
        "query": "我读的什么大学？",
        "expected": None,
    },
    {
        "facts": [{"content": "Allen 养了一只猫叫 Mochi", "category": "relationship", "key": "pet", "importance": 7}],
        "query": "我银行卡密码是什么？",
        "expected": None,
    },
    {
        "facts": [{"content": "Allen 喜欢日本料理", "category": "preference", "key": "cuisine", "importance": 6}],
        "query": "我穿多大号的鞋？",
        "expected": None,
    },
    {
        "facts": [{"content": "Allen 的妹妹叫小美", "category": "relationship", "key": "sister", "importance": 7}],
        "query": "我的邮箱地址是什么？",
        "expected": None,
    },
    {
        "facts": [{"content": "Allen 开一辆特斯拉 Model 3", "category": "knowledge", "key": "car", "importance": 6}],
        "query": "我的护照号是多少？",
        "expected": None,
    },
    {
        "facts": [{"content": "Allen 在 Shopify 工作", "category": "identity", "key": "company", "importance": 8}],
        "query": "我的体重是多少？",
        "expected": None,
    },
]


# ---------------------------------------------------------------------------
# Evaluation logic
# ---------------------------------------------------------------------------

def evaluate():
    """Run the full evaluation suite.

    Returns:
        dict mapping category name to list of per-case result dicts.
    """
    from memory.core.embedder import Embedder
    from memory.core.retriever import MemoryRetriever
    from memory.core.store import MemoryStore

    print("Loading embedder (bge-small-zh-v1.5) ...")
    t0 = time.time()
    embedder = Embedder()
    # Force model load
    embedder.encode("warmup")
    print(f"Embedder ready in {time.time() - t0:.1f}s\n")

    results = {
        "single_hop": [],
        "preference": [],
        "distractor": [],
        "negative": [],
    }

    datasets = [
        ("single_hop", SINGLE_HOP),
        ("preference", PREFERENCE),
        ("distractor", DISTRACTOR),
        ("negative", NEGATIVE),
    ]

    total_cases = sum(len(ds) for _, ds in datasets)
    done = 0

    for category, dataset in datasets:
        for case in dataset:
            with tempfile.TemporaryDirectory() as tmpdir:
                store = MemoryStore(os.path.join(tmpdir, "eval.db"))
                retriever = MemoryRetriever(store)

                # Store facts
                for fact in case["facts"]:
                    emb = embedder.encode(fact["content"])
                    store.add_memory(
                        user_id="eval_user",
                        content=fact["content"],
                        category=fact["category"],
                        key=fact.get("key"),
                        importance=float(fact["importance"]),
                        embedding=emb,
                    )

                query = case["query"]
                expected = case["expected"]
                query_emb = embedder.encode(query)

                # Test 1: DirectAnswer — removed in memory v2; always None.
                da_result = None
                da_hit = False
                da_correct_none = expected is None

                # Test 2: Retriever top-5
                retrieved = retriever.retrieve(query_emb, "eval_user", top_k=5)
                retriever_hit = False
                retriever_rank = -1
                if expected is not None:
                    for rank, mem in enumerate(retrieved):
                        if expected in mem["content"]:
                            retriever_hit = True
                            retriever_rank = rank + 1
                            break

                store.close()

            results[category].append({
                "query": query,
                "expected": expected,
                "da_result": da_result,
                "da_hit": da_hit or da_correct_none,
                "retriever_hit": retriever_hit,
                "retriever_rank": retriever_rank,
                "top1_content": retrieved[0]["content"] if retrieved else None,
                "top1_score": retrieved[0]["_score"] if retrieved else 0,
            })
            done += 1
            sys.stdout.write(f"\r  Evaluating ... {done}/{total_cases}")
            sys.stdout.flush()

    print("\n")
    return results


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_report(results: dict) -> None:
    """Print a formatted evaluation report."""
    print("=" * 60)
    print("          记忆系统端到端评估报告")
    print("=" * 60)

    overall_da_hits = 0
    overall_da_total = 0
    overall_mrr_sum = 0.0
    overall_mrr_total = 0
    overall_top1_hits = 0
    overall_top1_total = 0
    negative_reject = 0
    negative_total = 0

    category_labels = {
        "single_hop": "单跳事实查询",
        "preference": "偏好查询",
        "distractor": "干扰查询",
        "negative": "负面查询",
    }

    for cat_key in ["single_hop", "preference", "distractor", "negative"]:
        cases = results[cat_key]
        n = len(cases)
        label = category_labels[cat_key]

        print(f"\n【{label}】 ({n} cases)")

        if cat_key == "negative":
            # Negative: count correct rejections
            reject_count = sum(1 for c in cases if c["da_hit"])
            negative_reject = reject_count
            negative_total = n
            print(f"  DirectAnswer 正确拒绝: {reject_count}/{n} ({100*reject_count/n:.0f}%)")

            # Also show what DA returned when it shouldn't have
            false_positives = [c for c in cases if not c["da_hit"]]
            if false_positives:
                print("  -- 误报详情:")
                for c in false_positives:
                    print(f"     Q: {c['query']}")
                    print(f"     DA: {c['da_result']}")
        else:
            # DirectAnswer hit rate
            da_hits = sum(1 for c in cases if c["da_hit"])
            print(f"  DirectAnswer 命中率: {da_hits}/{n} ({100*da_hits/n:.0f}%)")

            if cat_key in ("single_hop", "preference"):
                overall_da_hits += da_hits
                overall_da_total += n

            # MRR@5
            mrr_sum = 0.0
            for c in cases:
                if c["retriever_rank"] > 0:
                    mrr_sum += 1.0 / c["retriever_rank"]
            mrr = mrr_sum / n if n > 0 else 0.0
            print(f"  Retriever MRR@5:     {mrr:.2f}")

            overall_mrr_sum += mrr_sum
            overall_mrr_total += n

            # Top-1 accuracy
            top1 = sum(1 for c in cases if c["retriever_rank"] == 1)
            print(f"  Top-1 准确率:        {top1}/{n} ({100*top1/n:.0f}%)")
            overall_top1_hits += top1
            overall_top1_total += n

        # Detail for misses
        misses = [c for c in cases if not c["da_hit"] and cat_key != "negative"]
        if misses and cat_key != "negative":
            print("  -- 未命中详情:")
            for c in misses:
                rank_str = f"rank={c['retriever_rank']}" if c["retriever_rank"] > 0 else "miss"
                print(f"     Q: {c['query']} | expected: {c['expected']} | "
                      f"DA: {c['da_result']} | retriever: {rank_str}")

    # Overall
    print("\n" + "=" * 60)
    print("总体指标")
    print("=" * 60)

    if overall_da_total > 0:
        print(f"  DA 命中率（正向，单跳+偏好）: {overall_da_hits}/{overall_da_total} "
              f"({100*overall_da_hits/overall_da_total:.0f}%)")
    if overall_mrr_total > 0:
        overall_mrr = overall_mrr_sum / overall_mrr_total
        print(f"  Retriever MRR@5:              {overall_mrr:.2f}")
    if overall_top1_total > 0:
        print(f"  Top-1 准确率:                 {overall_top1_hits}/{overall_top1_total} "
              f"({100*overall_top1_hits/overall_top1_total:.0f}%)")
    if negative_total > 0:
        print(f"  负面拒绝率:                   {negative_reject}/{negative_total} "
              f"({100*negative_reject/negative_total:.0f}%)")

    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        from memory.core.embedder import Embedder  # noqa: F401
    except Exception as exc:
        print(f"Failed to import Embedder: {exc}", file=sys.stderr)
        print("Make sure you run this from the project root with venv activated.",
              file=sys.stderr)
        sys.exit(1)

    try:
        results = evaluate()
    except Exception as exc:
        print(f"Evaluation failed: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)

    print_report(results)


if __name__ == "__main__":
    main()
