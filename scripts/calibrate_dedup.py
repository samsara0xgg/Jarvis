#!/usr/bin/env python3
"""Calibrate dedup cosine-similarity thresholds using 45 Chinese memory pairs.

Groups:
  - 15 duplicate pairs (same-meaning rephrasings) — should be flagged as duplicates
  - 15 related-but-different pairs — should NOT be merged
  - 15 unrelated pairs — should NOT be merged

Outputs distribution stats and precision/recall at various thresholds.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

# Allow importing from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory.embedder import Embedder


# ── Data ─────────────────────────────────────────────────────────────

duplicates = [
    ("Allen 喜欢喝拿铁", "Allen 最爱的饮品是拿铁咖啡"),
    ("Allen 住在温哥华", "Allen 的居住地是加拿大温哥华"),
    ("Allen 的妹妹叫小美", "Allen 有个妹妹名叫小美"),
    ("Allen 是软件工程师", "Allen 的职业是软件开发"),
    ("Allen 在 Shopify 上班", "Allen 目前在 Shopify 工作"),
    ("Allen 喜欢跑步", "Allen 平时爱好是跑步运动"),
    ("Allen 对花生过敏", "Allen 有花生过敏症"),
    ("Allen 养了一只猫叫 Mochi", "Allen 有一只名叫 Mochi 的猫"),
    ("Allen 开特斯拉 Model 3", "Allen 的车是特斯拉 Model 3"),
    ("Allen 喜欢听周杰伦", "Allen 最爱听周杰伦的歌"),
    ("Allen 不喜欢吃香菜", "Allen 讨厌香菜这种食物"),
    ("Allen 支持湖人队", "Allen 是 NBA 湖人队的球迷"),
    ("Allen 的生日是 3 月 15 日", "Allen 三月十五号生日"),
    ("Allen 女朋友叫 Sarah", "Allen 的女友名字是 Sarah"),
    ("Allen 喜欢日本料理", "Allen 最喜欢的菜系是日料"),
]

related_different = [
    ("Allen 喜欢喝拿铁", "Allen 喜欢喝绿茶"),
    ("Allen 住在温哥华", "Allen 去过东京旅游"),
    ("Allen 的妹妹叫小美", "Allen 的妈妈是老师"),
    ("Allen 是软件工程师", "Allen 在学机器学习"),
    ("Allen 在 Shopify 上班", "Allen 之前在 Google 实习"),
    ("Allen 喜欢跑步", "Allen 也喜欢游泳"),
    ("Allen 对花生过敏", "Allen 不喜欢吃辣"),
    ("Allen 养了一只猫叫 Mochi", "Allen 小时候养过一只狗"),
    ("Allen 开特斯拉 Model 3", "Allen 想买一辆自行车"),
    ("Allen 喜欢听周杰伦", "Allen 也听五月天的歌"),
    ("Allen 不喜欢吃香菜", "Allen 不喜欢吃苦瓜"),
    ("Allen 支持湖人队", "Allen 也看英超比赛"),
    ("Allen 的生日是 3 月 15 日", "Allen 的纪念日是 7 月 20 日"),
    ("Allen 女朋友叫 Sarah", "Allen 的好朋友叫 Tom"),
    ("Allen 喜欢日本料理", "Allen 昨天吃了韩国烤肉"),
]

unrelated = [
    ("Allen 喜欢喝拿铁", "今天天气很好"),
    ("Allen 住在温哥华", "Python 是一种编程语言"),
    ("Allen 的妹妹叫小美", "明天要开会"),
    ("Allen 是软件工程师", "苹果手机很贵"),
    ("Allen 在 Shopify 上班", "地球绕太阳转"),
    ("Allen 喜欢跑步", "股市今天涨了"),
    ("Allen 对花生过敏", "圣诞节快到了"),
    ("Allen 养了一只猫叫 Mochi", "火车比飞机慢"),
    ("Allen 开特斯拉 Model 3", "冰淇淋有很多口味"),
    ("Allen 喜欢听周杰伦", "北京是中国的首都"),
    ("Allen 不喜欢吃香菜", "数学是一门学科"),
    ("Allen 支持湖人队", "春天花会开"),
    ("Allen 的生日是 3 月 15 日", "海水是咸的"),
    ("Allen 女朋友叫 Sarah", "月亮绕地球转"),
    ("Allen 喜欢日本料理", "电影票涨价了"),
]


def cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two unit-norm vectors."""
    return float(np.dot(a, b))


def stats(scores: list[float]) -> dict[str, float]:
    """Return min/max/mean/median for a list of scores."""
    arr = np.array(scores)
    return {
        "min": float(arr.min()),
        "max": float(arr.max()),
        "mean": float(arr.mean()),
        "median": float(np.median(arr)),
    }


def main() -> None:
    print("Loading embedder (BAAI/bge-small-zh-v1.5) ...")
    embedder = Embedder()

    # Compute similarities
    print("Computing cosine similarities for 45 pairs ...\n")
    dup_scores = []
    rel_scores = []
    unr_scores = []

    for a, b in duplicates:
        dup_scores.append(cosine_sim(embedder.encode(a), embedder.encode(b)))
    for a, b in related_different:
        rel_scores.append(cosine_sim(embedder.encode(a), embedder.encode(b)))
    for a, b in unrelated:
        unr_scores.append(cosine_sim(embedder.encode(a), embedder.encode(b)))

    # Print distributions
    print("=" * 60)
    print("Distribution Summary")
    print("=" * 60)
    for name, scores in [
        ("Duplicates (should flag)", dup_scores),
        ("Related-different (should NOT flag)", rel_scores),
        ("Unrelated (should NOT flag)", unr_scores),
    ]:
        s = stats(scores)
        print(f"\n{name}:")
        print(f"  min={s['min']:.4f}  max={s['max']:.4f}  "
              f"mean={s['mean']:.4f}  median={s['median']:.4f}")
        print(f"  scores: {[f'{v:.4f}' for v in scores]}")

    # Threshold sweep
    print("\n" + "=" * 60)
    print("Threshold Sweep (positive=duplicates, negative=non-duplicates)")
    print("=" * 60)
    print(f"{'Threshold':>10} {'Precision':>10} {'Recall':>10} {'F1':>10}")
    print("-" * 44)

    negative_scores = rel_scores + unr_scores  # 30 non-duplicate pairs
    best_f1 = 0.0
    best_thresh = 0.0

    for thresh_int in range(40, 85, 5):
        thresh = thresh_int / 100.0
        tp = sum(1 for s in dup_scores if s >= thresh)
        fp = sum(1 for s in negative_scores if s >= thresh)
        fn = sum(1 for s in dup_scores if s < thresh)

        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

        print(f"{thresh:>10.2f} {precision:>10.4f} {recall:>10.4f} {f1:>10.4f}")

        if f1 > best_f1:
            best_f1 = f1
            best_thresh = thresh

    print(f"\nBest threshold: {best_thresh:.2f} (F1={best_f1:.4f})")

    # Current thresholds
    print("\n" + "=" * 60)
    print("Current thresholds vs recommendation")
    print("=" * 60)
    print(f"  _DEDUP_THRESHOLD_SAME_CAT  = 0.55 (current)")
    print(f"  _DEDUP_THRESHOLD_CROSS_CAT = 0.70 (current)")
    print(f"  Recommended same-cat       = {best_thresh:.2f}")
    print(f"  (cross-cat should stay higher, e.g. same-cat + 0.10-0.15)")


if __name__ == "__main__":
    main()
