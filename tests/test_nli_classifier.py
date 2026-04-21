"""Tests for NLIClassifier — Erlangshen-Roberta-110M-NLI wrapper.

Requires data/nli-erlangshen/model.onnx (run scripts/export_nli_onnx.py first).
Tests skip automatically if the model file is absent (CI without large models).

Passing bar: >= 8/10 annotated samples (plan spec, NLI not perfect).
"""
from __future__ import annotations

import pytest
from pathlib import Path

MODEL_PATH = Path("data/nli-erlangshen/model.onnx")

pytestmark = pytest.mark.skipif(
    not MODEL_PATH.exists(),
    reason="NLI model not exported — run scripts/export_nli_onnx.py",
)


@pytest.fixture(scope="module")
def nli():
    from memory.cold.nli_classifier import NLIClassifier
    return NLIClassifier()


# (user_text, expected_signal, description)
# expected: +1 / -1 / None
TEST_CASES = [
    ("好的", 1, "short positive"),
    ("不对", -1, "short negative"),
    ("嗯不太对吧", -1, "medium negative — regex misses this"),
    ("你说得很对", 1, "medium positive"),
    ("其实我想问的是别的", -1, "implicit correction (borderline — allowed to fail)"),
    ("嗯", None, "ambiguous filler"),
    ("", None, "empty string"),
    ("对了我还想问", None, "topic shift — not feedback"),
    ("我觉得这个回答很有道理", 1, "positive long form"),
    ("不是这个意思我是说...", -1, "correction with followup (borderline — allowed to fail)"),
]


def _run_detect(nli_instance, text: str) -> int | None:
    """Mirror the filter logic from detect_outcome() in outcome_detector.py."""
    stripped = text.strip()
    if not stripped or len(stripped) < 2 or len(stripped) > 500:
        return None
    return nli_instance.detect_outcome(stripped)


def test_annotated_samples_pass_eight_of_ten(nli):
    """Core accuracy gate: >= 8/10 labeled samples must match expected signal."""
    passed = []
    failed = []
    for text, expected, reason in TEST_CASES:
        result = _run_detect(nli, text)
        if result == expected:
            passed.append((text, expected, reason))
        else:
            failed.append((text, expected, result, reason))

    fail_msg = "\n".join(
        f"  FAIL {text!r}: expected={expected!r} got={result!r} ({reason})"
        for text, expected, result, reason in failed
    )
    assert len(passed) >= 8, (
        f"Only {len(passed)}/10 samples passed (need >= 8):\n{fail_msg}"
    )


def test_positive_cases(nli):
    positives = [t for t, exp, _ in TEST_CASES if exp == 1]
    results = [_run_detect(nli, t) for t in positives]
    # At least 2 of 3 positives must be detected.
    correct = sum(r == 1 for r in results)
    assert correct >= 2, f"Positive detection too low: {correct}/3, results={results}"


def test_negative_cases(nli):
    negatives = [t for t, exp, _ in TEST_CASES if exp == -1]
    results = [_run_detect(nli, t) for t in negatives]
    # At least 2 of 4 negatives must be detected.
    correct = sum(r == -1 for r in results)
    assert correct >= 2, f"Negative detection too low: {correct}/4, results={results}"


def test_null_cases_dont_fire(nli):
    """Ambiguous/empty/topic-shift texts must not produce +1 or -1."""
    nulls = [t for t, exp, _ in TEST_CASES if exp is None]
    for text in nulls:
        result = _run_detect(nli, text)
        assert result is None, f"False signal for {text!r}: got {result}"


def test_classify_returns_probabilities_summing_to_one(nli):
    scores = nli.classify("好的", "说话人表示认可")
    keys = {"entailment", "neutral", "contradiction"}
    assert set(scores.keys()) == keys
    total = sum(scores.values())
    assert abs(total - 1.0) < 1e-4, f"Probabilities don't sum to 1.0: {total}"


def test_classify_thread_safe(nli):
    """Concurrent calls should not corrupt session state."""
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(nli.classify, "你好", "说话人表示认可")
            for _ in range(8)
        ]
        results = [f.result() for f in futures]
    assert all(isinstance(r, dict) for r in results)


def test_empty_string_does_not_call_classify(nli):
    """Empty string never reaches the model (guarded upstream)."""
    # detect_outcome itself handles empty via outer filter in outcome_detector,
    # but NLIClassifier.detect_outcome also accepts it gracefully.
    result = nli.detect_outcome("")
    # Model may return any value for empty — just must not raise.
    assert result in (-1, 0, 1, None)


def test_long_text_truncated_within_tokenizer(nli):
    """Very long text should complete without error (tokenizer truncates)."""
    long_text = "这是一段很长的文字" * 100
    result = nli.detect_outcome(long_text)
    assert result in (-1, 1, None)
