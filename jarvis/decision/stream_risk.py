"""Versioned deterministic risk floor for the narrow no-tool stream route.

The classifier is deliberately independent of the legacy completion gate.
Missing context and negated action claims never become low-risk defaults.
This rule set requires quality evaluation before production activation.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from collections.abc import Callable

    from jarvis.shared.stream_emission import SegmentRisk

RULE_VERSION: Final = "routine-zh-en-v1"
CONTEXT_VERSION: Final = "response-risk-v1"
_MAX_SCAN_CHARS: Final = 8192
_HARD_CAP_MS: Final = 20.0


def _canonical(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _canonical(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            message = "snapshot mappings require string keys"
            raise TypeError(message)
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_canonical(item) for item in value), key=_json)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    message = f"unsupported snapshot value: {type(value).__name__}"
    raise TypeError(message)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def snapshot_content_hash(snapshot: object) -> str:
    """Hash actual frozen packet/context values; reject opaque fallback strings."""
    return hashlib.sha256(_json(_canonical(snapshot)).encode()).hexdigest()


@dataclass(frozen=True, kw_only=True)
class ResponseRiskContext:
    """Explicit complete inputs, pinned before provider request admission.

    ``none`` means known absence, ``unknown`` means unresolved; an absent L2
    entity is never sufficient to pick ``none``. The assembler of this context
    must include relevant action/confirmation recovery debt and typed history.
    """

    response_id: str
    turn_id: str
    user_request: str
    route: Literal["casual_or_explanatory", "action", "consequential", "unknown"]
    active_subject_ref: str
    linked_action_ids: tuple[str, ...]
    pending_action_risk: Literal["none", "consequential", "high", "unknown"]
    confirmation_state: Literal["none", "pending", "accepted_unconsumed", "unknown"]
    evidence_snapshot_hash: str
    attention_channel: str
    tools_offered: bool
    context_complete: bool
    unresolved_references: bool
    history_status: Literal["complete", "unknown"]
    schema_version: str = CONTEXT_VERSION

    @property
    def context_hash(self) -> str:
        """Identity includes every risk input, including the complete user request."""
        return snapshot_content_hash(self)


@dataclass(frozen=True)
class SegmentRiskResult:
    """Bounded local classification, including explicit failures and timing."""

    risk: SegmentRisk
    reasons: tuple[str, ...]
    rule_version: str
    elapsed_ms: float


_HIGH = re.compile(
    r"授权|批准|同意|许可|权限|密码|口令|密钥|凭证|认证|安全|绕过|转账|付款|支付|银行|账户|投资|"
    r"股票|买入|卖出|收益|贷款|税|医疗|药|剂量|服用|吞|两片|一片|治疗|诊断|症状|急救|癌|病|"
    r"法律|合同|诉讼|责任|违法|律师|自杀|自残|武器|炸弹|"
    r"\b(?:authori[sz]\w*|permission\w*|approv\w*|consent\w*|password\w*|credential\w*|"
    r"secret\w*|api\s*key|token\w*|authenticat\w*|security|bypass\w*|safe|safety|"
    r"bank\w*|payment\w*|pay|paid|transfer\w*|invest\w*|stock\w*|financ\w*|"
    r"buy|sell|loan\w*|tax\w*|medic\w*|drug\w*|pill\w*|tablet\w*|dose\w*|dosage\w*|treat\w*|"
    r"cancer\w*|disease\w*|cure\w*|swallow\w*|"
    r"diagnos\w*|symptom\w*|prescri\w*|legal\w*|contract\w*|liabil\w*|lawyer\w*|"
    r"suicid\w*|self.harm|weapon\w*|bomb\w*)\b",
    re.IGNORECASE,
)
_CONSEQUENTIAL = re.compile(
    r"完成|成功|通过|通过了|发[送出]|寄[送出]|邮件|文件|删|移除|清空|更改|修改|改动|设置|"
    r"更新|安装|卸载|重启|关机|启用|禁用|关闭|打开|保存|写入|读取|提交|合并|"
    r"推送|部署|执行|运行|处理好|修好|弄好|解决|验证|检查|测试|证据|证明|确认|"
    r"查到|查了|正在查|搜[索到]|记录显示|已经|马上|稍等|后台|任务|工具|"
    r"\b(?:complet\w*|done|success\w*|finish\w*|sent|send\w*|delet\w*|remov\w*|eras\w*|"
    r"open\w*|clos\w*|run|ran|sudo|proceed\w*|ready|"
    r"chang\w*|modif\w*|updat\w*|install\w*|uninstall\w*|restart\w*|reboot\w*|"
    r"enabl\w*|disabl\w*|turn\s+(?:on|off)|saved?|sav[ei]ng|writ\w*|wrote|"
    r"written|read\w*|commit\w*|merg\w*|push\w*|deploy\w*|execut\w*|running|"
    r"fix\w*|resolv\w*|verif\w*|check\w*|test\w*|evidence|prov\w*|confirm\w*|"
    r"look(?:ed|ing)\s+up|search\w*|according\s+to|already|soon|working\s+on|"
    r"task\w*|tool\w*|repository|file\w*|email\w*)\b",
    re.IGNORECASE,
)
_AMBIGUOUS = re.compile(
    r"[`*_#\[\]{}<>|\\]|\b(?:https?://|www\.|mailto:)|@|"
    r"^(?:若|如果|除非|只要|假如|倘若)|\b(?:if|unless|provided\s+that)\b",
    re.IGNORECASE,
)

_PERSONAL_OR_IMPERATIVE = re.compile(
    r"^(?:我|你|您|咱|请|别|不要|可以|已|没|不必|无需)|"
    r"\b(?:I|we|you|my|our|your|mine|ours|yours)\b|"
    r"^(?:take|do|use|try|make|go|let|allow|start|stop)\b",
    re.IGNORECASE,
)
_EXPLANATORY_FORM = re.compile(
    r"是|会|因此|所以|因为|由于|来自|取决于|意味着|表示|指的是|形成|吸收|释放|"
    r"分子|热量|温度|物质|能量|随着|例如|通常|"
    r"\b(?:is|are|was|were|means|refers|depends|because|therefore|"
    r"absorbs?|releases?|becomes?|contains?|causes?|forms?|consists?)\b",
    re.IGNORECASE,
)
_GREETINGS = frozenset(
    {
        "你好",
        "早上好",
        "晚上好",
        "早安",
        "晚安",
        "hello",
        "hi",
        "good morning",
        "good evening",
        "hello, good morning",
    }
)


def _supported_candidate(text: str) -> bool:
    """V1 admits plain explanations/greetings, not arbitrary unrecognized prose.

    This positive form guard complements the domain/action rules; a route label
    alone never establishes the meaning of off-topic model output. Quality and
    coverage still need a fixed live evaluation before enabling the route.
    """
    normalized = unicodedata.normalize("NFKC", text).strip()
    if normalized.casefold().rstrip(".,!?:;。") in _GREETINGS:
        return True
    if _PERSONAL_OR_IMPERATIVE.search(normalized):
        return False
    return _EXPLANATORY_FORM.search(normalized) is not None


def _text_risk(text: str) -> tuple[SegmentRisk, str]:  # noqa: PLR0911 - risk table
    normalized = unicodedata.normalize("NFKC", text)
    # Control/format characters cannot disguise a risky keyword between letters.
    if any(unicodedata.category(char) in {"Cf", "Cs", "Co", "Cn"} for char in normalized):
        return "unknown", "unsupported_text_encoding"
    if _HIGH.search(normalized):
        return "high_risk_claim", "sensitive_domain_or_authorization"
    if _CONSEQUENTIAL.search(normalized):
        return "consequential_claim", "action_evidence_or_progress"
    if _AMBIGUOUS.search(normalized):
        return "unknown", "structured_or_conditional_text"
    if not any(char.isalpha() for char in normalized):
        return "unknown", "no_meaningful_text"
    # V1 is evaluated only for Chinese/English; other scripts require buffering.
    if any(
        char.isalpha() and not ("a" <= char.lower() <= "z" or "\u3400" <= char <= "\u9fff")
        for char in normalized
    ):
        return "unknown", "unsupported_language"
    return "routine", "routine_text"


def _context_risk(context: ResponseRiskContext) -> tuple[SegmentRisk, str]:
    if (
        context.schema_version != CONTEXT_VERSION
        or context.context_complete is not True
        or context.tools_offered is not False
        or context.unresolved_references is not False
        or context.history_status != "complete"
        or not isinstance(context.linked_action_ids, tuple)
        or any(not isinstance(item, str) or not item for item in context.linked_action_ids)
        or re.fullmatch(r"[0-9a-f]{64}", context.evidence_snapshot_hash) is None
        or not context.response_id
        or not context.turn_id
        or not context.user_request.strip()
        or context.route == "unknown"
        or context.active_subject_ref in {"", "unknown"}
        or context.pending_action_risk == "unknown"
        or context.confirmation_state == "unknown"
        or context.attention_channel not in {"voice_notify", "queue_review", "silent_log"}
    ):
        return "unknown", "incomplete_or_unsupported_context"
    if context.pending_action_risk == "high":
        return "high_risk_claim", "pending_high_risk_action"
    if (
        context.route != "casual_or_explanatory"
        or context.active_subject_ref != "none"
        or context.linked_action_ids
        or context.pending_action_risk != "none"
        or context.confirmation_state != "none"
    ):
        return "consequential_claim", "consequential_context"
    risk, reason = _text_risk(context.user_request)
    return risk, "request_" + reason


class SegmentRiskClassifier:
    """Single bounded scan; errors/version/time overruns cannot grant permission."""

    def __init__(
        self,
        *,
        rule_version: str = RULE_VERSION,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Pin rule version and monotonic timing source."""
        self.rule_version = rule_version
        self._clock = clock

    def classify(self, text: str, context: ResponseRiskContext) -> SegmentRiskResult:
        """Take the monotonic maximum of complete route context and candidate."""
        started = self._clock()
        try:
            risk, reasons = self._classify(text, context)
        except Exception:  # noqa: BLE001 - classifier errors must buffer, ADR-0008 D2
            risk, reasons = "unknown", ("classifier_error",)
        elapsed_ms = (self._clock() - started) * 1000
        if not math.isfinite(elapsed_ms) or elapsed_ms < 0 or elapsed_ms >= _HARD_CAP_MS:
            risk, reasons = "unknown", ("classifier_deadline_exceeded",)
            elapsed_ms = _HARD_CAP_MS if not math.isfinite(elapsed_ms) else max(elapsed_ms, 0)
        return SegmentRiskResult(risk, reasons, self.rule_version, elapsed_ms)

    def _classify(
        self,
        text: str,
        context: ResponseRiskContext,
    ) -> tuple[SegmentRisk, tuple[str, ...]]:
        if self.rule_version != RULE_VERSION:
            return "unknown", ("unknown_rule_version",)
        if len(text) > _MAX_SCAN_CHARS or len(context.user_request) > _MAX_SCAN_CHARS:
            return "unknown", ("classification_input_bound",)
        floor, floor_reason = _context_risk(context)
        candidate, candidate_reason = _text_risk(text)
        if "unknown" in (floor, candidate):
            return "unknown", (floor_reason, candidate_reason)
        order: tuple[SegmentRisk, ...] = ("routine", "consequential_claim", "high_risk_claim")
        risk = max((floor, candidate), key=order.index)
        if risk == "routine" and not _supported_candidate(text):
            return "unknown", ("outside_evaluated_candidate_form",)
        return risk, (floor_reason, candidate_reason)
