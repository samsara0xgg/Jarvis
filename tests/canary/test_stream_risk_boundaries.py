"""Chinese/English risk counterexamples for the complete-context stream gate."""

# ruff: noqa: RUF001 - actual CJK and fullwidth text are classifier inputs

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from jarvis.decision.stream_gate import routine_stream_policy
from jarvis.decision.stream_risk import ResponseRiskContext, SegmentRiskClassifier

_CONTEXT = ResponseRiskContext(
    response_id="risk-table",
    turn_id="risk-turn",
    user_request="解释冰为什么融化。",
    route="casual_or_explanatory",
    active_subject_ref="none",
    linked_action_ids=(),
    pending_action_risk="none",
    confirmation_state="none",
    evidence_snapshot_hash="a" * 64,
    attention_channel="voice_notify",
    tools_offered=False,
    context_complete=True,
    unresolved_references=False,
    history_status="complete",
)


@pytest.mark.parametrize(
    ("text", "risk"),
    [
        ("冰从周围吸收热量。", "routine"),
        ("水分子因此更容易自由移动。", "routine"),
        ("Ice absorbs heat from its surroundings.", "routine"),
        ("Hello, good morning.", "routine"),
        ("温度升高时，", "routine"),
        ("已经发出去了。", "consequential_claim"),
        ("没有发送。", "consequential_claim"),
        ("并没有删除文件。", "consequential_claim"),
        ("I did not send it.", "consequential_claim"),
        ("I didn't delete it.", "consequential_claim"),
        ("I will change it now.", "consequential_claim"),
        ("测试已经全部通过。", "consequential_claim"),
        ("Everything is verified.", "consequential_claim"),
        ("I'm checking the repository now.", "consequential_claim"),
        ("稍等，马上就好了。", "consequential_claim"),
        ("I am working on that task.", "consequential_claim"),
        ("No evidence supports that yet.", "consequential_claim"),
        ("按这个执行。", "consequential_claim"),
        ("You approved it, so I can proceed.", "high_risk_claim"),
        ("尚未获得授权。", "high_risk_claim"),
        ("你的密码是，", "high_risk_claim"),
        ("没有泄露密钥。", "high_risk_claim"),
        ("Do not expose the API key.", "high_risk_claim"),
        ("This password is invalid.", "high_risk_claim"),
        ("It is safe.", "high_risk_claim"),
        ("Don't double the dose.", "high_risk_claim"),
        ("这个剂量比较合适。", "high_risk_claim"),
        ("不建议买入股票。", "high_risk_claim"),
        ("There is no legal liability.", "high_risk_claim"),
        ("我不能代表你承担法律责任。", "high_risk_claim"),
        ("若第一项成立，", "unknown"),
        ("If the first condition holds,", "unknown"),
        ("**Ordinary text.**", "unknown"),
        ("<tool>go</tool>", "consequential_claim"),
        ("s\u200bend it.", "unknown"),
        ("１２３。", "unknown"),
        ("Bonjour à tous.", "unknown"),
        ("", "unknown"),
        ("a" * 8193, "unknown"),
    ],
)
def test_bilingual_candidate_risk_table(text: str, risk: str) -> None:
    """Negation and missing subjects cannot erase action or domain risk."""
    assert SegmentRiskClassifier().classify(text, _CONTEXT).risk == risk


@pytest.mark.parametrize(
    "changes",
    [
        {"active_subject_ref": "unknown"},
        {"active_subject_ref": "task:real"},
        {"context_complete": False},
        {"unresolved_references": True},
        {"history_status": "unknown"},
        {"linked_action_ids": ("action-1",)},
        {"pending_action_risk": "unknown"},
        {"pending_action_risk": "consequential"},
        {"pending_action_risk": "high"},
        {"confirmation_state": "pending"},
        {"confirmation_state": "accepted_unconsumed"},
        {"confirmation_state": "unknown"},
        {"tools_offered": True},
        {"route": "unknown"},
        {"route": "action"},
        {"attention_channel": "ask_confirm"},
        {"schema_version": "future"},
        {"user_request": "替我发邮件。"},
        {"user_request": "删掉那个文件了吗？"},
        {"user_request": "Did you send it?"},
        {"user_request": "Can you change the setting?"},
        {"user_request": "这个药要吃多少？"},
        {"user_request": "Should I buy this stock?"},
        {"user_request": "解释一下合同责任。"},
        {"user_request": "关闭安全校验可以吗？"},
        {"user_request": "API key 是什么？"},
        {"user_request": ""},
    ],
)
def test_context_floor_blocks_even_neutral_candidate(changes: dict[str, Any]) -> None:
    """A neutral first sentence inherits consequential or incomplete input risk."""
    context = replace(_CONTEXT, **changes)
    assert SegmentRiskClassifier().classify("好的。", context).risk != "routine"
    assert (
        routine_stream_policy(context, preset_snapshot_hash="b" * 64).emission_mode == "full_text"
    )


def test_repeated_conditional_prefix_does_not_backtrack_past_classifier_deadline() -> None:
    """An 8 KiB adversarial token must buffer without the old quadratic regex."""
    result = SegmentRiskClassifier().classify("当" * 8192, _CONTEXT)
    assert result.risk == "unknown"
    assert result.elapsed_ms < 20
    assert "classifier_deadline_exceeded" not in result.reasons
