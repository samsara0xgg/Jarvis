#!/usr/bin/env python3
"""Calibrate DirectAnswer similarity threshold using the project embedder.

Measures cosine similarity distributions for positive (matching) and negative
(unrelated) queries against a set of simulated memories, then recommends an
optimal threshold for DirectAnswer.
"""
from __future__ import annotations

import sys

import numpy as np

# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

memories = [
    "Allen 住在加拿大温哥华",
    "Allen 喜欢喝拿铁",
    "Allen 的妹妹叫小美",
    "Allen 是软件工程师",
    "Allen 喜欢跑步",
    "Allen 的 WiFi 密码是 home2024",
    "Allen 养了一只猫叫 Mochi",
    "Allen 最喜欢的电影是星际穿越",
    "Allen 对花生过敏",
    "Allen 用 Python 和 TypeScript 写代码",
    "Allen 每天早上 7 点起床",
    "Allen 喜欢听周杰伦的歌",
    "Allen 的生日是 3 月 15 日",
    "Allen 开一辆特斯拉 Model 3",
    "Allen 不喜欢吃香菜",
    "Allen 在 Shopify 工作",
    "Allen 喜欢看 NBA，支持湖人队",
    "Allen 的女朋友叫 Sarah",
    "Allen 喜欢日本料理",
    "Allen 周末经常去 Stanley Park 跑步",
]

queries = [
    "我住在哪里？",
    "我喜欢喝什么咖啡？",
    "我妹妹叫什么？",
    "我是做什么工作的？",
    "我平时做什么运动？",
    "WiFi 密码是多少？",
    "我的猫叫什么名字？",
    "我最喜欢什么电影？",
    "我对什么过敏？",
    "我用什么编程语言？",
    "我几点起床？",
    "我喜欢听谁的歌？",
    "我的生日是哪天？",
    "我开什么车？",
    "我不喜欢吃什么？",
    "我在哪里上班？",
    "我支持哪个 NBA 球队？",
    "我女朋友叫什么？",
    "我喜欢吃什么菜系？",
    "我周末去哪里跑步？",
]

negative_queries = [
    "今天天气怎么样？",
    "帮我开灯",
    "现在几点了？",
    "给我讲个笑话",
    "明天会下雨吗？",
    "帮我设个闹钟",
    "播放音乐",
    "股票行情怎么样？",
    "帮我翻译一下",
    "最近有什么新闻？",
]

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    try:
        from memory.core.embedder import Embedder
    except Exception as exc:
        print(f"Failed to import Embedder: {exc}", file=sys.stderr)
        print("Make sure you run this from the project root with venv activated.", file=sys.stderr)
        sys.exit(1)

    try:
        embedder = Embedder()
    except Exception as exc:
        print(f"Failed to load embedding model: {exc}", file=sys.stderr)
        print("The model may not be downloaded yet. Run the app once to trigger download.", file=sys.stderr)
        sys.exit(1)

    # Encode everything
    print("Encoding memories and queries...")
    mem_matrix = embedder.encode_batch(memories)      # (20, dim)
    query_matrix = embedder.encode_batch(queries)      # (20, dim)
    neg_matrix = embedder.encode_batch(negative_queries)  # (10, dim)

    # Cosine matrices (vectors are already unit-normed)
    pos_cosine = query_matrix @ mem_matrix.T   # (20, 20)
    neg_cosine = neg_matrix @ mem_matrix.T     # (10, 20)

    # ---------------------------------------------------------------------------
    # Positive query analysis
    # ---------------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("POSITIVE QUERY ANALYSIS (query -> matching memory)")
    print("=" * 70)
    print(f"{'#':<3} {'Query':<25} {'Match':>6} {'Max':>6} {'Min':>6} {'Mean':>6} {'Top1?':>6}")
    print("-" * 70)

    match_scores = []
    top1_hits = 0
    margins = []

    for i in range(len(queries)):
        match_score = float(pos_cosine[i, i])
        row_max = float(np.max(pos_cosine[i]))
        row_min = float(np.min(pos_cosine[i]))
        row_mean = float(np.mean(pos_cosine[i]))
        top1_idx = int(np.argmax(pos_cosine[i]))
        hit = top1_idx == i

        # Margin: top-1 vs top-2
        sorted_scores = np.sort(pos_cosine[i])[::-1]
        margin = float(sorted_scores[0] - sorted_scores[1])
        margins.append(margin)

        match_scores.append(match_score)
        if hit:
            top1_hits += 1

        q_short = queries[i][:24]
        hit_str = "YES" if hit else "NO"
        print(f"{i:<3} {q_short:<25} {match_score:>6.3f} {row_max:>6.3f} {row_min:>6.3f} {row_mean:>6.3f} {hit_str:>6}")

    match_arr = np.array(match_scores)
    print(f"\nPositive match scores:  min={match_arr.min():.3f}  max={match_arr.max():.3f}  "
          f"mean={match_arr.mean():.3f}  median={float(np.median(match_arr)):.3f}")
    print(f"Top-1 accuracy: {top1_hits}/{len(queries)} ({100*top1_hits/len(queries):.0f}%)")

    # ---------------------------------------------------------------------------
    # Negative query analysis
    # ---------------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("NEGATIVE QUERY ANALYSIS (unrelated queries -> all memories)")
    print("=" * 70)
    print(f"{'#':<3} {'Query':<25} {'Max':>6} {'Min':>6} {'Mean':>6}")
    print("-" * 50)

    neg_max_scores = []
    for i in range(len(negative_queries)):
        row_max = float(np.max(neg_cosine[i]))
        row_min = float(np.min(neg_cosine[i]))
        row_mean = float(np.mean(neg_cosine[i]))
        neg_max_scores.append(row_max)
        q_short = negative_queries[i][:24]
        print(f"{i:<3} {q_short:<25} {row_max:>6.3f} {row_min:>6.3f} {row_mean:>6.3f}")

    neg_arr = np.array(neg_max_scores)
    print(f"\nNegative max scores:    min={neg_arr.min():.3f}  max={neg_arr.max():.3f}  "
          f"mean={neg_arr.mean():.3f}  median={float(np.median(neg_arr)):.3f}")

    # ---------------------------------------------------------------------------
    # Threshold analysis
    # ---------------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("THRESHOLD ANALYSIS (precision / recall at different thresholds)")
    print("=" * 70)
    thresholds = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]
    print(f"{'Threshold':>10} {'TP':>5} {'FP':>5} {'FN':>5} {'Precision':>10} {'Recall':>10} {'F1':>10}")
    print("-" * 60)

    best_threshold = thresholds[0]
    best_recall = 0.0

    for t in thresholds:
        tp = 0  # positive query, correct memory above threshold
        fp = 0  # negative query, any memory above threshold
        fn = 0  # positive query, correct memory below threshold

        for i in range(len(queries)):
            if match_scores[i] >= t:
                # Check if top-1 is correct
                if int(np.argmax(pos_cosine[i])) == i:
                    tp += 1
                else:
                    fp += 1
            else:
                fn += 1

        for i in range(len(negative_queries)):
            if neg_max_scores[i] >= t:
                fp += 1

        precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0
        recall = tp / len(queries) if len(queries) > 0 else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

        print(f"{t:>10.2f} {tp:>5} {fp:>5} {fn:>5} {precision:>10.3f} {recall:>10.3f} {f1:>10.3f}")

        if precision > 0.90 and recall > best_recall:
            best_recall = recall
            best_threshold = t

    print(f"\nRecommended threshold (precision>90%, max recall): {best_threshold:.2f}")

    # ---------------------------------------------------------------------------
    # Margin analysis
    # ---------------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("MARGIN ANALYSIS (top-1 vs top-2 score difference)")
    print("=" * 70)
    margin_arr = np.array(margins)
    print(f"Margins:  min={margin_arr.min():.3f}  max={margin_arr.max():.3f}  "
          f"mean={margin_arr.mean():.3f}  median={float(np.median(margin_arr)):.3f}")
    print(f"\n{'#':<3} {'Query':<25} {'Top1':>6} {'Top2':>6} {'Margin':>7}")
    print("-" * 55)
    for i in range(len(queries)):
        sorted_scores = np.sort(pos_cosine[i])[::-1]
        q_short = queries[i][:24]
        print(f"{i:<3} {q_short:<25} {sorted_scores[0]:>6.3f} {sorted_scores[1]:>6.3f} {margins[i]:>7.3f}")

    print("\nDone.")


if __name__ == "__main__":
    main()
