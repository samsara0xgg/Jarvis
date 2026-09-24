"""L4 Capability Execution — ToolRegistry + ActionLifecycle + handlers.

This module is the SINGLE place in `jarvis/` allowed to dispatch L4 tool
handlers.

Per spec.html §3.4 (`ActionRequest` / lifecycle / ToolRegistry) +
spec.html §3.5 (CallerPrincipal scoping) + ADR 0001 § Acceptance B
(lifecycle) + ADR 0019 (the flat `Tool` shape).

Two handler shapes coexist while the old-shape tools remain:

- `Tool` (ADR 0019) — a function of its arguments; the dispatcher emits
  every event, moves the lifecycle and serializes the payload.
- `ToolDefinition` — the handler writes its own `action.result_observed`
  and transitions the lifecycle terminal before returning
  (`search_notes` / `read_file` / `open_url` / `write_file`).

`tool_result` / `tool_error` JSON serializers are adapted verbatim from
`/Users/alllllenshi/Projects/jarvis-legacy/tools_v2/helpers.py` per
ADR § Reference sources.

`RuntimePaths` is consumed via a structural `RuntimePathsLike` Protocol
(see below) so this module does NOT import `jarvis.deployment` — that
would violate the `.importlinter` middle-layer sibling rule (execution
and deployment are independent siblings in `decision | execution |
surface | deployment`). `jarvis.runtime` (the composition root) passes a
real `jarvis.deployment.RuntimePaths` into the registry; it structurally
satisfies the Protocol.

Layer boundary (`.importlinter` + canary H13): stdlib only plus
`jarvis.shared` and `jarvis.state`. No imports from
`jarvis.constitution`, `jarvis.decision`, `jarvis.surface`,
`jarvis.deployment`, `jarvis.runtime`, `jarvis.cli`.
"""

from __future__ import annotations

import codecs
import ipaddress
import json
import logging
import math
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from jarvis.execution.path_resolver import TargetKind, load_file_targets_config
from jarvis.execution.path_resolver import resolve as resolve_path_target
from jarvis.shared import (
    ActionRequest,
    CallerPrincipal,
    RawResult,
    RawResultBundle,
    ResultSemantics,
    RiskLevel,
)
from jarvis.shared.action_admission import action_admission_guard
from jarvis.shared.text import truncate_utf8
from jarvis.state.authorized_dispatch_outbox import admit_authorized_dispatch
from jarvis.state.event_log import emit_event, iter_events_of_types
from jarvis.state.lifecycle_terminal import terminalize_action

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping, Sequence

    from jarvis.shared import Event


LOGGER = logging.getLogger(__name__)


# --- Public type aliases -----------------------------------------------------

# `RawResult` and `ResultSemantics` live in `jarvis.shared` since Step 0b of
# ADR-0002 (so L3 can import them without crossing the layer DAG). They are
# re-exported via `__all__` below for backward compatibility with Day-1
# callers that imported them from `jarvis.execution.tools`.


class RuntimePathsLike(Protocol):
    """Structural view of `jarvis.deployment.RuntimePaths` used by L4.

    L4 cannot import L6 (sibling layers in `.importlinter`). The composition
    root (`jarvis.runtime`, Step 17) passes a real `RuntimePaths` instance;
    it satisfies this Protocol structurally.

    Surface:
        - ``event_log`` — DB path.
        - ``artifacts_root`` — root artifact dir (`screen_look` saves its
          screenshots under it; `write_file` stages pending writes there).
    """

    @property
    def event_log(self) -> Path:
        """Path to the SQLite Event Log file."""
        ...

    @property
    def artifacts_root(self) -> Path:
        """Root artifact directory; per-run subdirs live underneath."""
        ...


# --- Exceptions --------------------------------------------------------------


class ToolRegistryError(Exception):
    """Base class for ToolRegistry-level failures."""


class DuplicateToolError(ToolRegistryError):
    """Raised when `register` is called twice for the same tool name."""


class UnknownToolError(ToolRegistryError):
    """Raised when `dispatch` receives an `ActionRequest.tool_name` that is not registered."""


class CallerNotAllowedError(ToolRegistryError):
    """Raised when an ActionRequest's `caller_principal` is not in `allowed_callers`.

    Defense-in-depth: the L3 Pre-action Gate normally catches this before
    dispatch; this raise is the L4 belt-and-braces enforcement so a bug
    in L3 can never silently dispatch a forbidden caller.
    """


class IllegalLifecycleTransition(Exception):  # noqa: N818 — name follows spec § Acceptance B vocabulary.
    """Raised on any state transition not in the 8-state DAG (or post-terminal mutation).

    Also raised by `ToolRegistry.dispatch` as the "dispatch precondition"
    failure when the action is not at `authorized` at the moment of
    dispatch.
    """


# --- ActionLifecycle 8-state machine (ADR § Acceptance B) -------------------

LifecycleState = Literal[
    "proposed",
    "authorized",
    "dispatched",
    "running",
    "result_observed",
    "failed",
    "timeout_assumed",
    "cancelled",
]
"""The 8 ActionLifecycle states (ADR § Acceptance B).

States are typed as a `Literal` so mypy can spot typos and so callers
can document their expected pre/post conditions precisely.
"""

# Allowed transitions per ADR § Acceptance B + step 6 spec. Terminal states
# (`result_observed`, `failed`, `timeout_assumed`, `cancelled`) have empty
# outgoing sets, so any transition out of them is illegal.
_VALID_TRANSITIONS: Final[Mapping[LifecycleState, frozenset[LifecycleState]]] = {
    "proposed": frozenset({"authorized", "cancelled"}),
    "authorized": frozenset({"dispatched", "cancelled"}),
    "dispatched": frozenset({"running", "failed", "cancelled"}),
    "running": frozenset({"result_observed", "failed", "timeout_assumed", "cancelled"}),
    "result_observed": frozenset(),
    "failed": frozenset(),
    "timeout_assumed": frozenset(),
    "cancelled": frozenset(),
}

_TERMINAL_STATES: Final[frozenset[LifecycleState]] = frozenset(
    {"result_observed", "failed", "timeout_assumed", "cancelled"}
)


class ActionLifecycle:
    """Per-action lifecycle FSM. Does NOT emit events — callers do that.

    Thread-safe via `threading.RLock` around the internal `dict`. Day-1
    contention is low (only L3 / L4 / L3-Result-Interpreter mutate;
    Timer callback only reads via emit), but the lock is cheap insurance
    against an L3 reinvocation racing an L4 verify in tests that exercise
    concurrent action_ids.
    """

    def __init__(self) -> None:
        """Construct an empty FSM (no actions registered yet)."""
        self._state: dict[str, LifecycleState] = {}
        self._lock = threading.RLock()

    def register(self, action_id: str) -> None:
        """Register a new action at initial state `proposed`.

        Idempotency: re-registering raises `IllegalLifecycleTransition`
        (a re-register would lose any prior progress and is almost
        certainly a bug; explicit re-use of an action_id should go
        through `cancelled` first).
        """
        with self._lock:
            if action_id in self._state:
                msg = (
                    f"action_id={action_id!r} is already registered "
                    f"(current state={self._state[action_id]!r})"
                )
                raise IllegalLifecycleTransition(msg)
            self._state[action_id] = "proposed"

    def transition(self, action_id: str, new_state: LifecycleState) -> None:
        """Move `action_id` from its current state to `new_state`.

        Validates via the `_VALID_TRANSITIONS` map; raises
        `IllegalLifecycleTransition` if `action_id` is unknown, currently
        terminal, or `new_state` is not in the allowed outgoing set.
        """
        with self._lock:
            current = self._state.get(action_id)
            if current is None:
                msg = f"action_id={action_id!r} is not registered"
                raise IllegalLifecycleTransition(msg)
            allowed = _VALID_TRANSITIONS[current]
            if new_state not in allowed:
                msg = (
                    f"illegal transition for action_id={action_id!r}: "
                    f"{current!r} → {new_state!r} (allowed from {current!r}: "
                    f"{sorted(allowed)!r})"
                )
                raise IllegalLifecycleTransition(msg)
            self._state[action_id] = new_state

    def state_of(self, action_id: str) -> LifecycleState | None:
        """Return the current state, or None if `action_id` is unknown."""
        with self._lock:
            return self._state.get(action_id)

    def is_terminal(self, action_id: str) -> bool:
        """Return True if the action is in one of the 4 terminal states."""
        with self._lock:
            state = self._state.get(action_id)
            if state is None:
                return False
            return state in _TERMINAL_STATES


# --- ToolDefinition dataclass -----------------------------------------------
#
# `RawResult` (and `ResultSemantics`) live in `jarvis.shared` since Step 0b;
# they are imported at the top of this module and re-exported via `__all__`.


if TYPE_CHECKING:
    ToolHandler = Callable[
        [ActionRequest, "sqlite3.Connection", RuntimePathsLike, ActionLifecycle],
        RawResult | RawResultBundle,
    ]


@dataclass(frozen=True)
class ToolDefinition:
    """One registered tool (ADR § Module map L4 row).

    Attributes:
        name: Stable string the LLM uses to call this tool.
        description: One-line description (shown in the tool list the
            LLM receives via `for_caller(...)`).
        allowed_callers: Frozen set of `CallerPrincipal` enum values that
            may dispatch this tool. Defense-in-depth: L3 Pre-action Gate
            checks this first; L4 dispatch raises `CallerNotAllowedError`
            if anyone slips through.
        risk_level: One of L0..L4 (matches `RiskLevel` in shared types).
            Compared against `EffectivePolicy.autonomy_ceiling` at the
            Pre-action Gate.
        result_semantics: The ``semantics`` tag the handler stamps on
            its ``action.result_observed`` row.
        input_schema: JSON schema describing the tool's arguments
            (Anthropic-style `{type, properties, required}` shape).
        handler: The callable that actually executes the tool.
        read_only: True if the tool performs no durable-state mutation.
            Feeds ``surface_for`` grouping and the Pre-emit scrub
            context (ADR-0011 D2).
        requires_entity: True if the Pre-action Gate must see a
            resolved ``target_entity_ref`` on the ActionRequest before
            letting this tool through. Feeds the gate's entity arm
            landing in Step 3 (ADR-0011 D3).
        requires_confirmation: True if dispatch needs a valid
            ``authorization_lease`` (feeds ADR-0012's confirmation
            template line). Must equal ``risk_rank(risk_level) >=
            risk_rank(confirmation_threshold)``; boot validation
            (``jarvis.decision.policy.validate_requires_confirmation``)
            enforces this so the two fields cannot drift.
        deferred: True if the tool stays off the model's tool list until
            ``tool_search`` finds it (ADR 0034).
    """

    name: str
    description: str
    allowed_callers: frozenset[CallerPrincipal]
    risk_level: RiskLevel
    result_semantics: ResultSemantics
    input_schema: Mapping[str, Any]
    handler: ToolHandler
    read_only: bool
    requires_entity: bool
    requires_confirmation: bool
    deferred: bool = False


# --- Tool (ADR 0019: the flat definition replacing ToolDefinition) ----------


@dataclass(frozen=True)
class ToolContext:
    """What a flat handler may reach beyond its arguments."""

    conn: sqlite3.Connection
    runtime_paths: RuntimePathsLike
    action_id: str


type FlatHandler = Callable[[Mapping[str, Any], ToolContext], Mapping[str, Any]]
"""A tool is a function of its arguments; everything else is the dispatcher's."""

type WorkStateRefresh = Callable[[Mapping[str, Any], ToolContext], dict[str, Any]]
"""ADR 0023: the runtime's one refresh workflow, injected into ``refresh_work_state``."""

type DailyReportRun = Callable[[Mapping[str, Any], ToolContext], dict[str, Any]]
"""ADR 0024: the runtime's daily-report workflow, injected into ``daily_work_report``."""

DEFAULT_MAX_RESULT_CHARS: Final[int] = 8192
"""Serialized-result budget of a flat tool that declares none (the 8 KiB every D5 tool used)."""

_MIN_WINDOW_CHARS: Final[int] = 80
"""Below this a head+tail window is all marker; the dispatcher shrinks the next string instead."""


class ToolError(Exception):
    """A tool-level failure the model should read as ``{"error": ..., "code": ...}``."""

    def __init__(self, message: str, *, code: str = "tool_error") -> None:
        """Keep ``code`` as the short tag ``action.result_observed.error`` carries."""
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Tool:
    """One registered tool (ADR 0019).

    ``handler`` takes the request's arguments and a :class:`ToolContext` and
    returns the result payload. The dispatcher emits every event, moves the
    lifecycle and serializes the payload: a return is an ``observation``, a
    raised :class:`ToolError` is an ``error`` observation, any other exception
    is an ``unexpected_error`` observation. ``max_result_chars`` bounds the
    serialized result: the longest string values are windowed head+tail until
    it fits and the payload is marked ``truncated``. ``requires_entity`` and
    ``requires_confirmation`` feed the Pre-action Gate arms that still exist;
    they leave with the audit chain. A ``deferred`` tool stays off the model's
    tool list until ``tool_search`` finds it (ADR 0034).
    """

    name: str
    description: str
    input_schema: Mapping[str, Any]
    handler: FlatHandler
    allowed_callers: frozenset[CallerPrincipal]
    risk_level: RiskLevel
    read_only: bool
    requires_entity: bool = False
    requires_confirmation: bool = False
    max_result_chars: int = DEFAULT_MAX_RESULT_CHARS
    deferred: bool = False


def tool(  # noqa: PLR0913 — one keyword per Tool field.
    *,
    description: str,
    input_schema: Mapping[str, Any],
    allowed_callers: frozenset[CallerPrincipal],
    risk_level: RiskLevel,
    read_only: bool,
    requires_entity: bool = False,
    requires_confirmation: bool = False,
    max_result_chars: int = DEFAULT_MAX_RESULT_CHARS,
) -> Callable[[FlatHandler], Tool]:
    """Build a :class:`Tool` from a handler; the function's name is the tool's."""

    def wrap(fn: FlatHandler) -> Tool:
        return Tool(
            name=fn.__name__,
            description=description,
            input_schema=input_schema,
            handler=fn,
            allowed_callers=allowed_callers,
            risk_level=risk_level,
            read_only=read_only,
            requires_entity=requires_entity,
            requires_confirmation=requires_confirmation,
            max_result_chars=max_result_chars,
        )

    return wrap


def _head_tail(text: str, keep: int) -> str:
    """Keep ``keep`` chars of ``text``: ~75% head, ~25% tail, an omission marker between."""
    if len(text) <= keep:
        return text
    # The marker's digit count is bounded by len(text), so the result never exceeds `keep`.
    room = keep - len(f"\n…[omitted {len(text)} chars]…\n")
    if room < _MIN_WINDOW_CHARS:
        return text[: max(0, keep)]
    head = room * 3 // 4
    tail = room - head
    return f"{text[:head]}\n…[omitted {len(text) - head - tail} chars]…\n{text[-tail:]}"


def _string_slots(node: Any, slots: list[tuple[Any, Any]]) -> None:  # noqa: ANN401 — walks any JSON-shaped value.
    """Collect every ``(container, key)`` that holds a str inside a JSON-shaped payload."""
    items: Any = node.items() if isinstance(node, dict) else enumerate(node)
    for key, value in items:
        if isinstance(value, str):
            slots.append((node, key))
        elif isinstance(value, (dict, list)):
            _string_slots(value, slots)


def _fit_result(payload: dict[str, Any], cap: int) -> dict[str, Any]:
    """Window the longest strings head+tail until ``tool_result(payload)`` fits ``cap``."""
    while (over := len(tool_result(payload)) - cap) > 0:
        slots: list[tuple[Any, Any]] = []
        _string_slots(payload, slots)
        candidates = [(c, k) for c, k in slots if len(c[k]) > _MIN_WINDOW_CHARS]
        if not candidates:
            break
        container, key = max(candidates, key=lambda ck: len(ck[0][ck[1]]))
        payload["truncated"] = True
        keep = max(_MIN_WINDOW_CHARS, len(container[key]) - over)
        container[key] = _head_tail(container[key], keep)
    return payload


def _observe(  # noqa: PLR0913 — the four handler arguments plus the definition and the uid the dispatcher holds.
    tool_def: Tool,
    action_request: ActionRequest,
    conn: sqlite3.Connection,
    runtime_paths: RuntimePathsLike,
    lifecycle: ActionLifecycle,
    *,
    running_event_uid: str,
) -> RawResult:
    """Run a flat tool and write its one terminal, ``action.result_observed``.

    The terminal row is the dispatcher's whatever the handler did, so the
    lifecycle never strands at ``running`` (ADR-0011 §5 "never a crash"): a
    non-``ToolError`` exception becomes an ``unexpected_error`` observation
    and is logged with its traceback.
    """
    action_id = action_request.action_id
    cap = tool_def.max_result_chars
    error: str | None = None
    message = ""
    payload: dict[str, Any] = {}
    tool_output = ""
    try:
        ctx = ToolContext(conn, runtime_paths, action_id)
        raw = tool_def.handler(action_request.arguments, ctx)
        payload = _fit_result(dict(raw), cap)
        tool_output = tool_result(payload)
    except ToolError as exc:
        error, message = exc.code, str(exc)
    except Exception as exc:
        # The terminal row is the dispatcher's whatever the handler did.
        LOGGER.exception("tool %s raised", tool_def.name)
        error = "unexpected_error"
        message = f"{tool_def.name}: unexpected {type(exc).__name__}: {exc}"
    if error is not None:
        tool_output = tool_result(_fit_result({"error": message, "code": error}, cap))
        payload = {"error": error}
    semantics: ResultSemantics = "observation" if error is None else "error"
    event_payload: dict[str, Any] = {
        "action_id": action_id,
        "semantics": semantics,
        "tool_output": tool_output,
    }
    if error is not None:
        event_payload["error"] = error
    terminalize_action(
        conn,
        event_type="action.result_observed",
        payload=event_payload,
        source_event_id=running_event_uid,
        correlation={"action_id": action_id},
    )
    lifecycle.transition(action_id, "result_observed")
    return RawResult(
        action_id=action_id,
        semantics=semantics,
        payload=payload,
        tool_output=tool_output,
        error=error,
    )


def _run_handler(  # noqa: PLR0913 — the four handler arguments plus the definition and the uid the dispatcher holds.
    tool_def: ToolDefinition | Tool,
    action_request: ActionRequest,
    conn: sqlite3.Connection,
    runtime_paths: RuntimePathsLike,
    lifecycle: ActionLifecycle,
    *,
    running_event_uid: str,
) -> RawResult | RawResultBundle:
    """Call the handler under its contract; a flat ``Tool`` gets the dispatcher's bookkeeping."""
    if isinstance(tool_def, ToolDefinition):
        return tool_def.handler(action_request, conn, runtime_paths, lifecycle)
    return _observe(
        tool_def, action_request, conn, runtime_paths, lifecycle,
        running_event_uid=running_event_uid,
    )


# --- JSON serializers (adapted from legacy tools_v2/helpers.py) -------------


def tool_error(message: object, *, code: str | None = None, **extra: object) -> str:
    """Serialize a tool failure as JSON.

    Verbatim shape from `jarvis-legacy/tools_v2/helpers.py` (ADR §
    Reference sources). Day-1 adds an optional `code` kwarg so handlers
    can carry a short machine-readable tag alongside the human message.

    Args:
        message: Human-readable error string (coerced via `str`).
        code: Optional machine-readable error tag (e.g. "artifact_missing").
        **extra: Optional additional keys to merge into the payload.

    Returns:
        JSON string of the form ``{"error": "<msg>", ...}`` with
        `ensure_ascii=False` so CJK characters survive intact.
    """
    payload: dict[str, Any] = {"error": str(message)}
    if code is not None:
        payload["code"] = code
    if extra:
        payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def tool_result(data: Mapping[str, Any] | None = None, **kwargs: object) -> str:
    """Serialize a tool success result as JSON.

    Verbatim shape from `jarvis-legacy/tools_v2/helpers.py`. Accepts a
    `data` mapping positional argument OR keyword arguments — kwargs are
    ignored if `data` is provided. Normalizes `Mapping` to `dict` before
    `json.dumps` so `MappingProxyType` / read-only mappings serialize.
    """
    if data is not None:
        payload: dict[str, Any] = dict(data)
    else:
        payload = dict(kwargs)
    return json.dumps(payload, ensure_ascii=False)


_OUTPUT_TAIL_BYTES: Final[int] = 2048
"""Maximum stderr tail `open_path` keeps on its error message."""


# --- get_current_time (F-Tier0) ---------------------------------------------


_WEEKDAYS_ZH: Final[tuple[str, ...]] = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
"""`datetime.weekday()` (Mon=0) indexed into the zh-CN weekday names."""

_DAY_PERIODS: Final[tuple[tuple[int, str], ...]] = (
    (6, "凌晨"),
    (9, "早上"),
    (12, "上午"),
    (13, "中午"),
    (18, "下午"),
    (24, "晚上"),
)
"""`(exclusive upper-bound hour, zh-CN day-period label)`, ascending."""

_SPOKEN_MINUTE_FILLER_BELOW: Final[int] = 10
"""Minutes below this take the spoken `零` filler (`10点零2分`)."""


def _spoken_day_period(hour: int) -> str:
    """Map a 24h hour to the zh-CN day-period prefix used by TTS."""
    for upper_bound, label in _DAY_PERIODS:
        if hour < upper_bound:
            return label
    return "晚上"


def _spoken_clock(hour: int, minute: int) -> str:
    """Render a 24h `(hour, minute)` as one idiomatic zh-CN spoken clock string.

    This field exists only to be spoken by TTS, so it follows speech
    convention rather than digit-for-digit transcription:

    - minute 0 says ``整`` (``上午10点整``), never ``10点0分``;
    - minutes 1-9 take the ``零`` filler (``10点零2分``) — dropping it
      makes the utterance wrong, not merely terse;
    - hour 0 is ``零点`` (``凌晨零点30分``); the naive 12h wrap would say
      ``凌晨12点``, which contradicts itself since ``12点`` reads as noon.

    Args:
        hour: Hour in 24h form, 0-23.
        minute: Minute, 0-59.

    Returns:
        The day-period prefix followed by the spoken clock reading.
    """
    period = _spoken_day_period(hour)
    hour_label = "零" if hour == 0 else str(hour % 12 or 12)
    if minute == 0:
        return f"{period}{hour_label}点整"
    if minute < _SPOKEN_MINUTE_FILLER_BELOW:
        return f"{period}{hour_label}点零{minute}分"
    return f"{period}{hour_label}点{minute}分"


@tool(
    description="Read the current local date and time (observation only).",
    input_schema={"type": "object", "properties": {}, "required": []},
    allowed_callers=frozenset({CallerPrincipal.REGEX_ROUTER, CallerPrincipal.JARVIS_LLM}),
    risk_level="L0",
    read_only=True,
)
def get_current_time(_args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
    """Read the system clock (spec §3.5.4); Tier 0's tool, jarvis_llm may call it too.

    The payload carries the machine keys ``iso`` / ``date`` / ``time`` /
    ``weekday`` plus the TTS-ready ``spoken_time`` / ``spoken_date`` the L5
    templates read.
    """
    now = datetime.now().astimezone()
    weekday = _WEEKDAYS_ZH[now.weekday()]
    return {
        "iso": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "weekday": weekday,
        "spoken_time": _spoken_clock(now.hour, now.minute),
        "spoken_date": f"{now.month}月{now.day}日{weekday}",
    }


# --- memo inbox (create_memo / list_memos) -----------------------------------

_MEMO_MAX_CHARS: Final[int] = 2000


@tool(
    description=(
        "Save a short memo to Allen's memo inbox for later review. "
        "Use when Allen says '记一下 X' / '备忘 X'."
    ),
    input_schema={
        "type": "object",
        "properties": {"text": {"type": "string", "description": "Memo text."}},
        "required": ["text"],
    },
    allowed_callers=frozenset({CallerPrincipal.REGEX_ROUTER, CallerPrincipal.JARVIS_LLM}),
    risk_level="L1",
    read_only=False,
)
def create_memo(args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Append one memo to the event log (`memo.captured`).

    Primary caller is the Tier 0 ``note_capture`` row (``/note ...``);
    the L2 event IS the memo store — ``list_memos`` folds it back.
    """
    text = str(args.get("text", "")).strip()[:_MEMO_MAX_CHARS]
    if not text:
        msg = "create_memo: text is empty"
        raise ToolError(msg, code="empty_text")
    memo_event = emit_event(
        ctx.conn,
        type="memo.captured",
        payload={"text": text, "action_id": ctx.action_id},
        source_event_id=_get_running_event_uid(ctx.conn, ctx.action_id),
        correlation={"action_id": ctx.action_id},
    )
    return {"memo_event_uid": memo_event.event_uid, "text": text}


@tool(
    description="List every saved memo, oldest first, with capture time. No arguments.",
    input_schema={"type": "object", "properties": {}, "required": []},
    allowed_callers=frozenset({CallerPrincipal.REGEX_ROUTER, CallerPrincipal.JARVIS_LLM}),
    risk_level="L0",
    read_only=True,
)
def list_memos(_args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Fold every `memo.captured` event into a numbered, dated list."""
    lines: list[str] = []
    for event in iter_events_of_types(ctx.conn, ("memo.captured",)):
        stamp = datetime.fromtimestamp(event.ts_epoch_ms / 1000).astimezone()
        lines.append(f"{len(lines) + 1}. [{stamp:%m-%d %H:%M}] {event.payload.get('text', '')}")
    return {"count": len(lines), "rendered": "\n".join(lines) if lines else "还没有备忘录。"}


# --- open_path handler --------------------------------------------------------

_OPEN_PATH_DEFAULT_TARGET_KIND: Final[str] = "any"
_OPEN_PATH_DEFAULT_APP: Final[str] = "default"
_OPEN_PATH_SUBPROCESS_TIMEOUT_S: Final[float] = 10.0


@dataclass(frozen=True)
class _OpenPathArgs:
    """Parsed + validated arguments for `open_path_handler`."""

    query: str
    target_kind: TargetKind
    app: Literal["default", "vscode"]


def _parse_open_path_args(arguments: Mapping[str, Any]) -> _OpenPathArgs:
    """Validate `open_path` arguments and return a typed bundle.

    Raises (`KeyError` for the missing required `query`, `TypeError` for a
    wrong type, an empty/whitespace-only query, or an out-of-enum value)
    rather than returning a sentinel — the caller, `open_path_handler`,
    catches both and turns them into a `tool_error(code="invalid_argument")`
    RawResult (see `list_tasks_handler`'s `invalid_argument` shape for the
    convention this mirrors). The schema's `enum`/`required` should catch
    most of this first for `jarvis_llm` callers, but Tier 0's literal-string
    `args` (spec §17) — and a Tier 0 capture group that strips to an empty
    string, e.g. "帮我打开 。" — bypass schema validation entirely, so the
    handler must defend here rather than let an unhandled exception strand
    the lifecycle at `running`.
    """
    query = arguments["query"]
    if not isinstance(query, str) or not query.strip():
        msg = f"open_path: query must be a non-empty string (got {query!r})"
        raise TypeError(msg)

    target_kind = arguments.get("target_kind", _OPEN_PATH_DEFAULT_TARGET_KIND)
    if target_kind not in ("file", "folder", "any"):
        msg = f"open_path: target_kind must be one of file/folder/any (got {target_kind!r})"
        raise TypeError(msg)

    app = arguments.get("app", _OPEN_PATH_DEFAULT_APP)
    if app not in ("default", "vscode"):
        msg = f"open_path: app must be one of default/vscode (got {app!r})"
        raise TypeError(msg)

    return _OpenPathArgs(query=query, target_kind=target_kind, app=app)


def _build_open_argv(path: Path, app: Literal["default", "vscode"]) -> tuple[list[str], str]:
    """Build the `open`/`open -a` argv per the app + extension rules.

    - `app == "vscode"` (file or folder) -> `open -a <editor_app> <path>`,
      unconditionally — an explicit "用 VS Code 打开" request always wins.
    - `app == "default"` and `path` is a file whose extension is in
      `editor_extensions` -> also `open -a <editor_app> <path>`.
    - Everything else (folders, non-editor files) -> the macOS default
      handler, `open <path>`.

    Returns `(argv, app_used)` where `app_used` is `editor_app` or the
    literal string `"default"` — the latter feeds the success payload's
    `app_used` field verbatim.
    """
    config = load_file_targets_config()
    if app == "vscode":
        return ["open", "-a", config.editor_app, str(path)], config.editor_app
    if path.is_file() and path.suffix.lstrip(".").lower() in config.editor_extensions:
        return ["open", "-a", config.editor_app, str(path)], config.editor_app
    return ["open", str(path)], "default"


# open_path is L1 — a subprocess side effect (`open <path>`), so it sits
# one rung above the L0 read-only observation tools even though it emits
# no claim. `app` stays a plain default/vscode enum rather than an
# arbitrary bundle-id string — the only Day-1 override is "force VS Code",
# matching the two Tier 0 patterns in config/tier0_patterns.yaml.
_OPEN_PATH_INPUT_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Spoken name or description of the file or folder to open.",
        },
        "target_kind": {
            "type": "string",
            "enum": ["file", "folder", "any"],
            "description": "Restrict the match to a file, a folder, or either. Default 'any'.",
        },
        "app": {
            "type": "string",
            "enum": ["default", "vscode"],
            "description": (
                "'default' uses the macOS default handler (or the configured "
                "editor for editor_extensions files); 'vscode' forces Visual "
                "Studio Code regardless of extension. Default 'default'."
            ),
        },
    },
    "required": ["query"],
}


@tool(
    description=(
        "Open a file or folder on Allen's Mac by spoken name (bookmark "
        "alias, partial filename, or description). Use for '打开 X' / "
        "'用 VS Code 打开 X' requests."
    ),
    input_schema=_OPEN_PATH_INPUT_SCHEMA,
    allowed_callers=frozenset({CallerPrincipal.REGEX_ROUTER, CallerPrincipal.JARVIS_LLM}),
    risk_level="L1",
    read_only=False,
    # requires_entity stays False deliberately: open_path keeps its own
    # resolve-then-act contract internally — it *is* a resolver caller.
)
def open_path(args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Open a file or folder on Allen's Mac by spoken name (spec §17 companion tool).

    "打开 X" / "用 VS Code 打开 X" — the query is resolved via
    :func:`jarvis.execution.path_resolver.resolve` (pure, no events; see
    that module for the bookmark / Spotlight / one-level-scan strategy),
    then handed to macOS `open`. Errors: ``invalid_argument`` (bad
    query / target_kind / app), ``target_not_found``, ``open_failed``.
    """
    try:
        parsed = _parse_open_path_args(args)
    except (KeyError, TypeError) as exc:
        msg = f"open_path: {exc}"
        raise ToolError(msg, code="invalid_argument") from exc

    target = resolve_path_target(parsed.query, parsed.target_kind, ctx.conn)
    if target is None:
        msg = f"open_path: no file/folder matched {parsed.query!r}"
        raise ToolError(msg, code="target_not_found")

    argv, app_used = _build_open_argv(target.path, parsed.app)
    try:
        proc = subprocess.run(  # noqa: S603 — argv list, no shell; path comes only from resolve()'s home-scoped candidates.
            argv,
            timeout=_OPEN_PATH_SUBPROCESS_TIMEOUT_S,
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        msg = f"open_path: subprocess failed to start: {exc}"
        raise ToolError(msg, code="open_failed") from exc
    if proc.returncode != 0:
        stderr_tail = (proc.stderr or "").strip()[-_OUTPUT_TAIL_BYTES:]
        msg = f"open_path: `open` exited {proc.returncode}: {stderr_tail}"
        raise ToolError(msg, code="open_failed")

    return {
        "opened_name": target.display_name,
        "opened_path": str(target.path),
        "app_used": app_used,
        "target_kind": parsed.target_kind,
    }


# --- ADR-0011 D5 read-only tools (search_notes / read_file / read_clipboard) -
#
# All three are L0, sync, `result_semantics="observation"`. Two shared
# helpers below cover the "emit exactly one action.result_observed +
# terminal-transition lifecycle" contract every sync handler in this
# module follows.


def _emit_tool_observation(  # noqa: PLR0913 — all kwargs are the shared sync-handler success shape (conn/lifecycle/action_id/running_event_uid/payload/semantics); splitting them into a bundle defeats the point of a shared helper, same rationale as `_emit_tool_error`.
    *,
    conn: sqlite3.Connection,
    lifecycle: ActionLifecycle,
    action_id: str,
    running_event_uid: str,
    payload: Mapping[str, Any],
    semantics: ResultSemantics = "observation",
) -> RawResult:
    """Shared success exit for the ADR-0011 D5 tools — observations AND acks alike.

    NIT-FIX 8, ADR-0011 §12: despite the name, this is not
    observation-only. ``semantics`` defaults to ``"observation"`` (every D5 tool but
    ``open_url``, ADR-0011 §3 D5's result_semantics column). ``open_url``
    passes ``semantics="ack"`` — it is the one D5 tool whose result is an
    Execution Claim ``executed`` rather than an observation (the browser
    opening is not verified) — so it shares this same emit+transition
    shape instead of duplicating it.
    """
    tool_output_str = tool_result(payload)
    terminalize_action(
        conn,
        event_type="action.result_observed",
        payload={
            "action_id": action_id,
            "semantics": semantics,
            "tool_output": tool_output_str,
        },
        source_event_id=running_event_uid,
        correlation={"action_id": action_id},
    )
    lifecycle.transition(action_id, "result_observed")
    return RawResult(
        action_id=action_id,
        semantics=semantics,
        payload=payload,
        tool_output=tool_output_str,
        error=None,
    )


_TOOL_ERROR_MESSAGE_MAX_BYTES: Final[int] = 8192
"""Same 8 KiB order as every other D5 tool's output cap (MUST-FIX 1b,
ADR-0011 §12). `_emit_tool_error` is the single choke point every sync
handler's failure exit goes through — capping HERE, once, means no
handler can write an unbounded `tool_output` / `action.result_observed`
row by interpolating a raw exception's `str()` verbatim (the uncapped
`f"...: {exc}"` idiom appears at half a dozen call sites; this bounds
all of them without touching any of them individually)."""


def _emit_tool_error(  # noqa: PLR0913 — all kwargs are the shared sync-handler failure shape (conn/lifecycle/action_id/running_event_uid/code/message); splitting them into a bundle defeats the point of a shared helper.
    *,
    conn: sqlite3.Connection,
    lifecycle: ActionLifecycle,
    action_id: str,
    running_event_uid: str,
    code: str,
    message: str,
) -> RawResult:
    """Shared error exit: `action.result_observed(error)` + terminal-transition.

    Used by the three ADR-0011 D5 tools AND `open_path_handler` — same
    "emit exactly one action.result_observed, transition the lifecycle
    terminal, on every exit path" contract every sync L4 handler in
    this module follows (the invariant `_get_running_event_uid`
    documents at its call sites).

    `message` is capped at `_TOOL_ERROR_MESSAGE_MAX_BYTES` (MUST-FIX 1b,
    ADR-0011 §12 / spec §3.5.11's bounded-payload rule) — a handler
    that interpolates a raw exception's `str()` can otherwise write an
    unbounded row into durable state (an upstream that echoes its
    request body, or simply a pathological error message).
    """
    message_bytes = message.encode("utf-8")
    if len(message_bytes) > _TOOL_ERROR_MESSAGE_MAX_BYTES:
        capped_text, undelivered_bytes, _lossy = truncate_utf8(
            message_bytes[:_TOOL_ERROR_MESSAGE_MAX_BYTES], len(message_bytes),
        )
        message = f"{capped_text}…[truncated {undelivered_bytes} bytes]"
    tool_output_str = tool_error(message, code=code)
    terminalize_action(
        conn,
        event_type="action.result_observed",
        payload={
            "action_id": action_id,
            "semantics": "error",
            "tool_output": tool_output_str,
            "error": code,
        },
        source_event_id=running_event_uid,
        correlation={"action_id": action_id},
    )
    lifecycle.transition(action_id, "result_observed")
    return RawResult(
        action_id=action_id,
        semantics="error",
        payload={"error": code},
        tool_output=tool_output_str,
        error=code,
    )


# --- search_notes ------------------------------------------------------------

_SEARCH_NOTES_DEFAULT_MAX_RESULTS: Final[int] = 5
_SEARCH_NOTES_MAX_RESULTS_CAP: Final[int] = 10

_SEARCH_NOTES_LINE_MAX_BYTES: Final[int] = 512
"""Per-matched-line cap (MUST-FIX 1, ADR-0011 §12). A vault note can
contain one pathological line (e.g. a pasted JSON blob, measured at
2,000,000+ chars in the wild) many times larger than any sane search
snippet; without this cap that ONE line would consume the entire
``_SEARCH_NOTES_TOTAL_MAX_BYTES`` budget below and crowd out every
other match. Codepoint-safe cut via `jarvis.shared.text.truncate_utf8`."""

_SEARCH_NOTES_TOTAL_MAX_BYTES: Final[int] = 8192
"""Aggregate cap across all returned rows — same 8 KiB order as the
`read_file`/`read_clipboard` sibling caps. The per-line cap above
already bounds the worst case to roughly
``_SEARCH_NOTES_MAX_RESULTS_CAP * _SEARCH_NOTES_LINE_MAX_BYTES`` (10 *
512 = 5120 today), but this is the hard ceiling that still holds if
either of those two numbers changes independently later."""

DEFAULT_OBSIDIAN_VAULT_ROOT: Final[Path] = Path("~/Documents/Obsidian Vault").expanduser()
"""Fallback vault root when the composition root passes none in (e.g. the
two existing integration tests that call ``build_default_registry()``
with no arguments). Production always overrides this with
``tools.obsidian.vault_root`` (ADR-0011 D7, `jarvis.runtime`).

NIT-FIX 8, ADR-0011 §12: single source of truth — `jarvis.runtime`
imports this constant (an L4 -> higher-layer import direction
`.importlinter` allows) rather than holding its own copy of the
literal, so the two layers can't drift apart."""

_SEARCH_NOTES_INPUT_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Case-insensitive search text. Whitespace-separated tokens "
                "must ALL appear in a line for it to match."
            ),
        },
        "max_results": {
            "type": "integer",
            "minimum": 1,
            "description": "Maximum number of matching lines to return. Default 5, capped at 10.",
        },
    },
    "required": ["query"],
}


def _resolved_within(path: Path, root: Path) -> bool:
    """Containment check: is ``path`` (resolved) under ``root`` (already resolved)?

    Mirrors `jarvis.execution.path_resolver._under_home` — resolve
    first (following symlinks), then test — so a symlinked ``*.md``
    that points outside ``root`` is caught even though 3.12's
    ``rglob`` already refuses to descend into a symlinked *directory*
    on its own (SHOULD-FIX 6, ADR-0011 §12).
    """
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved == root or root in resolved.parents


def _cap_rows_total_bytes(rows: list[str], max_bytes: int) -> list[str]:
    """Enforce an aggregate output cap across rendered rows (MUST-FIX 1).

    The caller's per-row cap already bounds any single row; this guards
    the SUM. Walks ``rows`` in order, keeping whole rows until the next
    one would exceed ``max_bytes``, then truncates that ONE row
    (codepoint-safe, same marker convention as the per-row cap) and
    drops everything after it.

    Shared by ``search_notes`` and ``web_search`` — both render a list
    of rows whose count is bounded but whose total size is not.
    """
    capped: list[str] = []
    used = 0
    for row in rows:
        row_bytes = row.encode("utf-8")
        if used + len(row_bytes) <= max_bytes:
            capped.append(row)
            used += len(row_bytes)
            continue
        # The marker is part of the output, so it has to come OUT of the
        # remaining budget rather than be appended past it — otherwise
        # the sum overshoots `max_bytes` by the marker's own length
        # every time the cap fires. Size the reservation with
        # `len(row_bytes)`: the real `undelivered` is always <= that, so
        # it never renders more digits than budgeted for.
        remaining = max_bytes - used
        marker_budget = len(f"…[truncated {len(row_bytes)} bytes]".encode())
        keep = remaining - marker_budget
        if keep > 0:
            text, undelivered, _lossy = truncate_utf8(row_bytes[:keep], len(row_bytes))
            capped.append(f"{text}…[truncated {undelivered} bytes]")
        break
    return capped


def _make_search_notes_handler(vault_root: Path) -> ToolHandler:  # noqa: C901 — one linear validate/search/rank pass; splitting scatters the fail-fast checks from the loop they guard.
    """Bind ``vault_root`` into a ``search_notes`` handler closure (ADR-0011 D7).

    ``search_notes`` is the only ADR-0011 D5 tool that needs a config
    value the ``ActionRequest`` / ``RuntimePathsLike`` surface doesn't
    already carry. Per the ADR-0011 build brief, L4 handlers do not
    load YAML themselves — the composition root
    (``jarvis.runtime.bootstrap_runtime_app``) reads
    ``tools.obsidian.vault_root`` once at registry-build time and
    closes over it here, the same seam a test uses to point the tool
    at a ``tmp_path`` fixture instead of Allen's real vault.

    No index (spec: vault is small) — every call walks the tree fresh.
    A missing vault root is an empty result with a note, not an error
    (Allen may rename the vault). A matched ``*.md`` that resolves
    outside ``vault_root`` (a symlink escape) is silently skipped
    (SHOULD-FIX 6). A note whose bytes are not valid UTF-8 is skipped
    from line-matching and its path collected under
    ``payload["non_utf8_files"]`` instead of narrating mojibake
    (SHOULD-FIX 7).
    """

    def _handler(  # noqa: C901, PLR0912 — one linear validate/search/rank pass (containment + encoding + per-line + total-cap guards all live in the loop they protect); splitting them out scatters the fail-fast checks from the data they guard, same rationale as the outer function's own noqa.
        action_request: ActionRequest,
        conn: sqlite3.Connection,
        runtime_paths: RuntimePathsLike,  # noqa: ARG001 — signature uniformity.
        lifecycle: ActionLifecycle,
    ) -> RawResult:
        running_event_uid = _get_running_event_uid(conn, action_request.action_id)
        action_id = action_request.action_id

        query = action_request.arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            return _emit_tool_error(
                conn=conn,
                lifecycle=lifecycle,
                action_id=action_id,
                running_event_uid=running_event_uid,
                code="invalid_argument",
                message=f"search_notes: query must be a non-empty string (got {query!r})",
            )

        max_results = _SEARCH_NOTES_DEFAULT_MAX_RESULTS
        max_results_arg = action_request.arguments.get("max_results")
        if (
            isinstance(max_results_arg, (int, float))
            and not isinstance(max_results_arg, bool)
            and math.isfinite(max_results_arg)
        ):
            # SHOULD-FIX 6, ADR-0011 §12: `json.loads` accepts `Infinity`/
            # `NaN` in the LLM tool-arg path, and `int(inf)` raises
            # OverflowError — the `isfinite` guard folds that shape into
            # "ignore, use the default", same as any other malformed
            # `max_results_arg` this branch already ignores silently.
            max_results = int(max_results_arg)
        max_results = max(1, min(max_results, _SEARCH_NOTES_MAX_RESULTS_CAP))

        if not vault_root.is_dir():
            payload: dict[str, Any] = {
                "results": [],
                "note": f"vault root not found: {vault_root} (Allen may have renamed it)",
            }
            return _emit_tool_observation(
                conn=conn,
                lifecycle=lifecycle,
                action_id=action_id,
                running_event_uid=running_event_uid,
                payload=payload,
            )

        tokens = [tok.lower() for tok in query.split() if tok]
        results: list[str] = []
        non_utf8_files: list[str] = []
        vault_root_resolved = vault_root.resolve()
        for md_path in sorted(vault_root.rglob("*.md")):
            if len(results) >= max_results:
                break
            if not _resolved_within(md_path, vault_root_resolved):
                continue  # SHOULD-FIX 6: symlinked *.md escaping the vault.
            try:
                raw = md_path.read_bytes()
            except OSError:
                continue
            text, _undelivered, lossy = truncate_utf8(raw, len(raw))
            if lossy:
                # SHOULD-FIX 7: don't narrate mojibake as real content —
                # flag the file instead of returning garbled lines.
                non_utf8_files.append(str(md_path))
                continue
            for line in text.splitlines():
                if len(results) >= max_results:
                    break
                if not all(tok in line.lower() for tok in tokens):
                    continue
                line_bytes = line.strip().encode("utf-8")
                line_text, line_undelivered, _lossy = truncate_utf8(
                    line_bytes[:_SEARCH_NOTES_LINE_MAX_BYTES], len(line_bytes),
                )
                if line_undelivered > 0:
                    line_text += f"…[truncated {line_undelivered} bytes]"
                results.append(f"{md_path} — {line_text}")

        results = _cap_rows_total_bytes(results, _SEARCH_NOTES_TOTAL_MAX_BYTES)
        payload = {"results": results}
        if non_utf8_files:
            payload["non_utf8_files"] = non_utf8_files
        return _emit_tool_observation(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_id,
            running_event_uid=running_event_uid,
            payload=payload,
        )

    return _handler


# --- read_file -----------------------------------------------------------------

_READ_FILE_MAX_BYTES: Final[int] = 8192
"""Output cap (8 KiB), ADR-0011 D5. A per-tool module constant, not a
schema field (§11 defer table: ``max_output_bytes`` deferred)."""

_READ_FILE_ENTITY_PREFIX: Final[str] = "file:"

_READ_FILE_INPUT_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "target": {
            "type": "string",
            "description": (
                "Spoken name or description of the file to read. Resolved to a "
                "canonical path before reading (never used as a raw path)."
            ),
        },
    },
    "required": ["target"],
}


def read_file_handler(
    action_request: ActionRequest,
    conn: sqlite3.Connection,
    runtime_paths: RuntimePathsLike,  # noqa: ARG001 — signature uniformity.
    lifecycle: ActionLifecycle,
) -> RawResult:
    """L0 `read_file` — the first `requires_entity=True` tool (ADR-0011 D5).

    Reads the RESOLVED canonical path off
    `action_request.target_entity_ref` — NEVER `arguments["target"]`,
    the raw free text the LLM supplied. That is the entire point of
    D4's resolve-on-propose contract and invariant I2 (no fabricated
    entity IDs): by the time this handler runs, the Pre-action Gate has
    already required a trusted ``"file:<abs-path>"`` ref, so the raw
    argument is never even read here.

    `target_entity_ref is None` cannot pass the gate for a
    `requires_entity=True` tool (ADR-0011 D3), but this handler checks
    it anyway — a handler must be safe standing alone, not merely
    behind a gate that happens to always run first in production.

    Text files only: a NUL byte anywhere in the `_READ_FILE_MAX_BYTES`
    prefix marks the file binary (same heuristic `git` uses) and the
    handler refuses rather than returning decoded garbage. Output is
    capped at 8 KiB, cut at the last valid UTF-8 codepoint boundary
    (never mid-character), with an explicit ``…[truncated N bytes]``
    marker appended when the file is larger. A file whose bytes are
    not valid UTF-8 at all (e.g. GBK) still returns an observation —
    best-effort lossy-decoded — but with `payload["encoding"] ==
    "lossy"` so the caller knows not to trust it as real text
    (SHOULD-FIX 7, ADR-0011 §12).
    """
    running_event_uid = _get_running_event_uid(conn, action_request.action_id)
    action_id = action_request.action_id

    ref = action_request.target_entity_ref
    if ref is None or not ref.startswith(_READ_FILE_ENTITY_PREFIX):
        return _emit_tool_error(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_id,
            running_event_uid=running_event_uid,
            code="no_resolved_target",
            message="read_file: no resolved target_entity_ref (the gate should have refused this)",
        )

    path = Path(ref[len(_READ_FILE_ENTITY_PREFIX) :])
    if not path.is_file():
        return _emit_tool_error(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_id,
            running_event_uid=running_event_uid,
            code="file_not_found",
            message=f"read_file: {path} does not exist or is not a regular file",
        )

    try:
        total_bytes = path.stat().st_size
        with path.open("rb") as fh:
            prefix = fh.read(_READ_FILE_MAX_BYTES)
    except OSError as exc:
        return _emit_tool_error(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_id,
            running_event_uid=running_event_uid,
            code="read_failed",
            message=f"read_file: {exc}",
        )

    if b"\x00" in prefix:
        return _emit_tool_error(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_id,
            running_event_uid=running_event_uid,
            code="binary_file",
            message=f"read_file: {path} looks binary, refusing to read it as text",
        )

    text, undelivered_bytes, lossy = truncate_utf8(prefix, total_bytes)
    if undelivered_bytes > 0:
        text += f"…[truncated {undelivered_bytes} bytes]"

    payload: dict[str, Any] = {
        "path": str(path),
        "content": text,
        "truncated": undelivered_bytes > 0,
        "total_bytes": total_bytes,
    }
    if lossy:
        # SHOULD-FIX 7: `path` is not valid UTF-8 (e.g. GBK) — tell the
        # caller explicitly rather than shipping a clean-looking
        # observation full of mojibake the LLM would narrate as real
        # content.
        payload["encoding"] = "lossy"
    return _emit_tool_observation(
        conn=conn,
        lifecycle=lifecycle,
        action_id=action_id,
        running_event_uid=running_event_uid,
        payload=payload,
    )


# --- read_clipboard --------------------------------------------------------------

_CLIPBOARD_TIMEOUT_S: Final[float] = 5.0


@tool(
    description="Read the current macOS clipboard's text contents. No arguments.",
    input_schema={"type": "object", "properties": {}, "required": []},
    allowed_callers=frozenset({CallerPrincipal.REGEX_ROUTER, CallerPrincipal.JARVIS_LLM}),
    risk_level="L0",
    read_only=True,
)
def read_clipboard(_args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
    """`pbpaste`. An empty clipboard is a valid empty observation, not an error."""
    try:
        proc = subprocess.run(
            ["pbpaste"],  # noqa: S607 — `pbpaste` resolved via PATH, matches mdfind/open precedent.
            timeout=_CLIPBOARD_TIMEOUT_S,
            capture_output=True,
            text=False,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        msg = f"read_clipboard: pbpaste failed to run: {exc}"
        raise ToolError(msg, code="clipboard_read_failed") from exc
    if proc.returncode != 0:
        stderr_tail = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        msg = f"read_clipboard: pbpaste exited {proc.returncode}: {stderr_tail}"
        raise ToolError(msg, code="clipboard_read_failed")
    raw = proc.stdout or b""
    return {"content": raw.decode("utf-8", errors="replace"), "total_bytes": len(raw)}


# --- SSRF egress guard (ADR-0011 D5) -----------------------------------------
#
# Shared by `web_fetch` and `open_url` — both hand a URL Allen (or the LLM)
# did not type into a terminal to something that reaches the network / the
# GUI. Refusing here is an in-handler guard refuse (ADR-0011 §5: "not a gate
# refuse — the URL is an argument, not an entity"), never a gate-level
# `requires_entity` concern and never an uncaught exception.

_ALLOWED_URL_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})

_IPV4_LITERAL_CHARS: Final[frozenset[str]] = frozenset("0123456789.")
"""Character set of an IPv4-dotted-literal-*shaped* host (MUST-FIX 1,
ADR-0011 §12) — deliberately includes bare-digit strings (`"0"`,
`"2130706433"`) and short forms (`"127.1"`), not just 4-part dotted
quads. Every one of those shapes is exactly what a browser's WHATWG URL
parser recognizes as "ends in a number" and converts to an IPv4 address
(decimal, or octal per-part on a leading zero) — never as a DNS
hostname. Treating them the same way here (strict-parse-or-refuse,
never fall through to `getaddrinfo`) is what closes the bypass."""


def _strict_ip_literal(
    host: str,
) -> tuple[bool, ipaddress.IPv4Address | ipaddress.IPv6Address | None]:
    """Return `(is_literal_shaped, addr)`.

    `is_literal_shaped=True, addr=None` means `host` LOOKS like an IP
    literal — digits-and-dots only, or contains a `:` (bracketed IPv6)
    — but failed a STRICT `ipaddress` parse. `0177.0.0.1` is exactly
    this: `socket.getaddrinfo` reads the leading zero as decimal
    (`177.0.0.1`, public) while every WHATWG URL parser (every browser,
    and `open_url`'s downstream `open`) reads it as octal (`127.0.0.1`,
    loopback) — CPython's `ipaddress.IPv4Address` has rejected leading
    zeros since 3.9.5, which is exactly the WHATWG-compatible reading.
    A literal-shaped host must never reach `getaddrinfo`, which would
    silently paper over the ambiguity in the resolver's favour.

    `is_literal_shaped=False` means `host` is an ordinary DNS hostname
    — untouched, resolved via `getaddrinfo` as before.

    IPv6 zone suffixes (`fe80::1%en0`) are stripped at the first literal
    `%` before parsing — `ipaddress.IPv6Address` rejects them outright,
    and the zone plays no part in which address a scoped literal names.
    """
    if host and set(host) <= _IPV4_LITERAL_CHARS:
        try:
            return True, ipaddress.IPv4Address(host)
        except ValueError:
            return True, None
    if ":" in host:
        try:
            return True, ipaddress.IPv6Address(host.split("%", 1)[0])
        except ValueError:
            return True, None
    return False, None


def _resolve_hostname_ips(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve `host` to its address(es), literal IPs parsed directly.

    Everything else goes via `socket.getaddrinfo` (real DNS). This is
    the default `resolver` `validate_egress_url` closes over; the
    ADR-0011 §7 SSRF acceptance table substitutes a canned mapping here
    instead (a hostname with both a public and a private A record cannot
    be reproduced against real DNS in a hermetic test).

    An IP-literal-shaped host (`_strict_ip_literal`) is parsed strictly
    and NEVER handed to `getaddrinfo` (MUST-FIX 1, ADR-0011 §12) — a
    literal that fails strict parsing returns an empty list (refused)
    rather than falling through to the resolver's own, looser
    normalization of legacy forms (decimal `2130706433`, hex
    `0x7f000001`, mixed `0x7f.0.0.1` are NOT digits-and-dots-only shaped
    and are unaffected — `getaddrinfo` already normalizes those to
    their dotted form identically to a browser, so no bypass exists
    there). A host that fails to resolve at all returns an empty list
    (the guard then refuses — an unresolvable host cannot be proven
    safe).
    """
    is_literal_shaped, literal_addr = _strict_ip_literal(host)
    if is_literal_shaped:
        return [] if literal_addr is None else [literal_addr]
    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return []
    addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    seen: set[str] = set()
    for info in infos:
        raw_ip = str(info[4][0])
        if raw_ip in seen:
            continue
        seen.add(raw_ip)
        try:
            addrs.append(ipaddress.ip_address(raw_ip.split("%", 1)[0]))
        except ValueError:
            continue
    return addrs


_NAT64_WELL_KNOWN_PREFIX: Final[ipaddress.IPv6Network] = ipaddress.IPv6Network("64:ff9b::/96")
"""RFC 6052 NAT64 well-known prefix. An address in this block is a
translated IPv4 address embedded in the low 32 bits — judging the /96
prefix itself (`is_global` says True; it is not IANA special-purpose
"reserved" in any way `ipaddress` models cheaply) tells you nothing;
judging the EMBEDDED address is the only thing that means anything
(MUST-FIX 4, ADR-0011 §12)."""


def _is_globally_routable(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Allowlist predicate — permit only a globally routable address.

    MUST-FIX 4, ADR-0011 §12. Inverted from the original denylist enumeration
    (loopback/link-local/private/unspecified), which left
    `100.64.0.0/10` — CGNAT, and the exact range Allen's Tailscale
    tailnet (including the RPi that runs Jarvis) lives on — reachable
    purely by omission: `ipaddress` classifies it as neither
    `is_loopback`, `is_link_local`, `is_private`, nor `is_unspecified`.

    `is_global` alone is not sufficient either — empirically (not just per
    the stdlib docs) it says `True` for: IPv4/IPv6 multicast
    (`224.0.0.1`, `ff02::1`), the deprecated IPv6 site-local block
    (`fec0::1`, `fec0::/10`), and any address in the NAT64 well-known
    prefix REGARDLESS of what it embeds (`64:ff9b::7f00:1` embeds
    `127.0.0.1`). Each gets an explicit refusal below; the NAT64 case
    recurses on the embedded IPv4 address instead of trusting the /96
    prefix, so a real NAT64-translated public address is not refused
    just for sharing the prefix.

    An IPv4-mapped IPv6 address (`::ffff:127.0.0.1`) is unwrapped to its
    IPv4 form first so it is judged by the exact same rule as the
    literal it represents — empirically `is_global` already agrees with
    the unwrapped form for every address family tried here, but the
    unwrap is kept as the documented, not-accidental, invariant.
    """
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if isinstance(addr, ipaddress.IPv6Address) and addr in _NAT64_WELL_KNOWN_PREFIX:
        embedded = ipaddress.IPv4Address(addr.packed[-4:])
        return _is_globally_routable(embedded)
    if addr.is_multicast:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.is_site_local:
        return False
    return addr.is_global


def _is_blocked_address(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for anything NOT globally routable.

    See `_is_globally_routable` (MUST-FIX 4, ADR-0011 §12) for the
    allowlist this inverts.
    """
    return not _is_globally_routable(addr)


@dataclass(frozen=True)
class _EgressUrlParts:
    """Scheme/host/port plus the canonical re-serialized URL string."""

    scheme: str
    host: str
    port: int | None
    canonical: str


def _parse_egress_url(url: str) -> tuple[_EgressUrlParts | None, str]:
    r"""Parse `url` into `(parts, reason)` — `parts is None` iff refused.

    `reason` names why, used verbatim by `validate_egress_url`.

    This is the ROOT-CAUSE fix (ADR-0011 §12 MUST-FIX 1/2): the bug
    class underlying both fixes is "the validated string is not the
    used string" — `validate_egress_url` parses with `urlsplit`, then
    the ORIGINAL string gets handed to a second parser (macOS
    LaunchServices/the browser for `open_url`, `httpx` for `web_fetch`)
    that can read it differently (backslash-as-slash in a WHATWG
    "special scheme" authority is the concrete case: `urlsplit` puts
    everything before the last `\@` into userinfo and the rest into
    host; the browser reinterprets the backslash as a path separator
    and lands on a completely different host).

    `parts.canonical` is rebuilt from ONLY the parsed scheme/host/port/
    path/query — userinfo and fragment are dropped unconditionally,
    never forwarded to `open` or `httpx`. `validate_egress_url` and
    `_normalize_egress_url` both call this SAME function, so the string
    a caller ends up acting on is always byte-identical to the one the
    guard's scheme/address checks ran against.

    A URL that does not survive a re-parse of its own canonical form
    with IDENTICAL scheme/host/port is refused outright (`parts is
    None`) rather than trusted — this is the literal "anything that
    does not survive parse → re-serialize → reparse identically is
    refused" rule; in practice this never fires for a legitimate URL
    (host/port can't contain any of the delimiters being reassembled
    around them) and exists as a named invariant, not a vector this
    project has observed.
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None, f"malformed URL {url!r}"

    host = parsed.hostname
    if not host:
        return None, "URL has no hostname"

    try:
        port = parsed.port
    except ValueError as exc:
        return None, f"invalid port in URL: {exc}"

    scheme = parsed.scheme.lower()
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    canonical = urlunsplit((scheme, netloc, parsed.path or "/", parsed.query, ""))

    reparsed = urlsplit(canonical)
    try:
        reparsed_port = reparsed.port
    except ValueError:
        return None, "URL does not survive canonical re-serialization (port)"
    if reparsed.scheme != scheme or reparsed.hostname != host or reparsed_port != port:
        return None, "URL does not survive canonical re-serialization (host/scheme mismatch)"

    return _EgressUrlParts(scheme=scheme, host=host, port=port, canonical=canonical), ""


def _normalize_egress_url(url: str) -> str | None:
    """Canonical re-serialized form of an already-guard-allowed `url`.

    The ONLY string a call site may act on (ADR-0011 §12 MUST-FIX 1/2
    root cause; see `_parse_egress_url`). `None` means `url` cannot be
    canonicalized — should not happen for a URL `validate_egress_url`
    just allowed (both call the identical parser), but callers check
    for it and refuse rather than ever fall back to the raw string.
    """
    parts, _reason = _parse_egress_url(url)
    return parts.canonical if parts is not None else None


def validate_egress_url(
    url: str,
    *,
    resolver: Callable[
        [str], Sequence[ipaddress.IPv4Address | ipaddress.IPv6Address]
    ] = _resolve_hostname_ips,
) -> tuple[bool, str]:
    """SSRF egress guard shared by `web_fetch` and `open_url` (ADR-0011 D5).

    Pure aside from the injected `resolver` (real DNS by default; the
    acceptance script substitutes a canned lookup). Returns
    `(allowed, reason)` — `reason` is `""` when `allowed` and otherwise
    names exactly what tripped, for the caller's error observation.

    Checks, in order (each is a real bypass if skipped, per ADR-0011 D5):

    1. The URL must parse into a canonical form that survives its own
       re-parse (`_parse_egress_url`) — a malformed URL, one with no
       hostname, or an unparseable port is refused before anything
       else is inspected.
    2. Scheme must be `http`/`https`. `file:`/`ftp:`/`data:`/
       `javascript:`/anything else is refused — this matters most for
       `open_url`, where macOS `open` will happily launch a non-http
       scheme.
    3. EVERY address `resolver` returns is checked, not just the first —
       a host with both a public and a private A record is refused.

    Callers that walk redirects (`web_fetch`) MUST call this again on
    every hop's `Location` — this function only ever sees one URL. A
    caller that gets `allowed=True` back MUST fetch/open
    `_normalize_egress_url(url)`, never `url` itself (see
    `_parse_egress_url`'s docstring for why).
    """
    parts, reason = _parse_egress_url(url)
    if parts is None:
        return False, reason

    if parts.scheme not in _ALLOWED_URL_SCHEMES:
        return False, f"scheme {parts.scheme!r} is not in the http/https allowlist"

    addrs = resolver(parts.host)
    if not addrs:
        return False, f"host {parts.host!r} did not resolve to any address"

    for addr in addrs:
        if _is_blocked_address(addr):
            return False, f"host {parts.host!r} resolves to blocked address {addr}"

    return True, ""


# --- web_search ----------------------------------------------------------------

DEFAULT_WEB_SEARCH_MAX_RESULTS: Final[int] = 5
"""Default `max_results` absent both a request argument and a
`tools.web.search_max_results` config override (ADR-0011 D7). Public so
`jarvis.runtime` can fall back to it — same single-source-of-truth
reasoning as `DEFAULT_OBSIDIAN_VAULT_ROOT` (NIT-FIX 8, ADR-0011 §12)."""

_WEB_SEARCH_MAX_RESULTS_CAP: Final[int] = 8
"""Hard ceiling (ADR-0011 D5) — NOT configurable, unlike the default
above. Applies regardless of what the request argument or config asks
for."""

DEFAULT_WEB_SEARCH_PROVIDER: Final[str] = "ddgs"
"""Default `tools.web.search_provider`.

`ddgs` (unofficial DuckDuckGo scraping) stays the DEFAULT only because
it needs no credential — it is not the recommended choice. It returns
link-plus-blurb, so any actual content costs a second `web_fetch`
iteration, and it is routinely rate-limited or blocked outright: turn
Te3815a16 burned three of its five tool-loop iterations on it and got
`No results found` from the last one. Point this at `exa` or `tavily`
(with `search_api_key_env` set) and search returns page text directly,
which is both better grounded and cheaper in loop iterations."""

_WEB_SEARCH_RESULT_TEXT_CAP: Final[int] = 1500
"""Per-result text budget, in characters, requested from a text-bearing
provider and enforced locally regardless of what it returns. Bounds any
ONE result; `_WEB_SEARCH_TOTAL_MAX_BYTES` bounds the sum."""

_WEB_SEARCH_TOTAL_MAX_BYTES: Final[int] = 8192
"""Aggregate cap across all rendered rows — same 8 KiB order as every
other tool's output cap. Without it, 8 results x full page text would
dwarf the rest of the decision packet."""

DEFAULT_WEB_TIMEOUT_S: Final[float] = 20.0
"""Shared `web_search`/`web_fetch` timeout absent a `tools.web.timeout_s`
config override. ADR-0011 D5's prose gives the two tools DIFFERENT
timeouts (15s / 20s) but D7's shipped config schema has only ONE
`timeout_s` key under `tools.web` — deliberately unprefixed, unlike its
`search_max_results`/`fetch_max_bytes` siblings, which reads as intent
to share one knob. Step 6 resolves the conflict by applying the single
configured value to both tools (ADR-0011 §12 errata candidate)."""

_WEB_SEARCH_INPUT_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Search query text.",
        },
        "max_results": {
            "type": "integer",
            "minimum": 1,
            "description": "Maximum number of results to return. Default 5, capped at 8.",
        },
    },
    "required": ["query"],
}


def _ddgs_search_backend(
    query: str,
    max_results: int,
    *,
    timeout_s: float,
) -> list[tuple[str, str, str]]:
    """Real DuckDuckGo backend via the `ddgs` package (ADR-0011 D5).

    One function, as D5 explicitly asks for, so a keyed API is a
    config-sized swap later — nothing above this function knows or
    cares that DuckDuckGo is behind it. Returns `(title, url, snippet)`
    tuples. The acceptance script monkeypatches this name directly
    rather than mocking `ddgs` internals, so it never makes a real
    network call.
    """
    # Imported here, not at module top, so a `ddgs` import error surfaces as
    # this tool's own error observation rather than failing
    # `import jarvis.execution.tools` for every caller.
    from ddgs import DDGS  # noqa: PLC0415

    # NIT-FIX 8, ADR-0011 §12: `int(timeout_s)` floors any sub-1s config
    # value to 0 — `max(1, ...)` keeps a deliberately-short timeout at a
    # sane floor instead of silently becoming "no timeout" / "instant
    # timeout" depending on how `ddgs` treats `0`.
    with DDGS(timeout=max(1, int(timeout_s))) as client:
        rows = client.text(query, max_results=max_results)
    return [
        (str(row.get("title", "")), str(row.get("href", "")), str(row.get("body", "")))
        for row in rows
    ]


def _collapse_ws(text: str) -> str:
    """Squeeze runs of whitespace to single spaces.

    Extracted page text arrives with the source document's newlines and
    indentation intact. Those bytes count against
    `_WEB_SEARCH_RESULT_TEXT_CAP` while carrying no meaning, and one
    result rendered across many lines would also break the one-row-per-
    result shape the results list promises.
    """
    return " ".join(text.split())


class _KeyedSearchBackend(Protocol):
    """A `web_search` backend that needs a credential (Exa, Tavily)."""

    def __call__(
        self, query: str, max_results: int, *, timeout_s: float, api_key: str,
    ) -> list[tuple[str, str, str]]: ...


def _exa_search_backend(
    query: str,
    max_results: int,
    *,
    timeout_s: float,
    api_key: str,
) -> list[tuple[str, str, str]]:
    """Exa `/search` with page text inlined (`contents.text`).

    The point of a text-bearing provider is that ONE tool call answers
    "what does the web say about X" — the LLM does not have to spend a
    second tool-loop iteration on `web_fetch` to see any actual content.
    That matters directly: the loop bound is 5, and the search-then-
    fetch dance is what exhausted it in turn Te3815a16.

    `highlights` (Exa's query-relevant passages) is preferred over
    `text` (the page from its top) for the same reason Tavily's
    `content` beats its `raw_content`: against a per-result budget, a
    page's opening bytes are nav chrome, not the answer. That reasoning
    is EVIDENCED for Tavily and only INFERRED here — this backend has
    never run against a real Exa key, so treat the field preference as
    unverified.
    """
    response = httpx.post(
        "https://api.exa.ai/search",
        headers={"x-api-key": api_key},
        json={
            "query": query,
            "numResults": max_results,
            "contents": {
                "text": {"maxCharacters": _WEB_SEARCH_RESULT_TEXT_CAP},
                "highlights": True,
            },
        },
        timeout=timeout_s,
    )
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    return [
        (
            str(row.get("title") or ""),
            str(row.get("url") or ""),
            _exa_row_text(row),
        )
        for row in rows
        if isinstance(row, dict)
    ]


def _exa_row_text(row: Mapping[str, Any]) -> str:
    """Prefer an Exa row's `highlights` over its `text` (see caller)."""
    highlights = row.get("highlights")
    if isinstance(highlights, list) and highlights:
        return " … ".join(str(h) for h in highlights if h)
    return str(row.get("text") or "")


def _tavily_search_backend(
    query: str,
    max_results: int,
    *,
    timeout_s: float,
    api_key: str,
) -> list[tuple[str, str, str]]:
    """Tavily `/search`, taking its relevance-selected `content`.

    `content` is Tavily's query-relevant extract; `raw_content` is the
    whole parsed page. `raw_content` was tried first and was WRONG:
    against a per-result budget of `_WEB_SEARCH_RESULT_TEXT_CAP`, a
    page's opening bytes are nav chrome, so the budget bought
    "[Skip to content](#main) ![logo]…" while `content` for the same
    result read "operated by Air Canada and China Eastern Airlines and
    the flight time is 13 hours and 50 minutes" — the actual answer, at
    ~1.3 KB. So `content` is requested and preferred, `raw_content` is
    not asked for at all (it is billed payload we would discard), and
    the fallback stays only for a row that somehow carries one and not
    the other. A result whose full page IS needed can be handed to
    `web_fetch` — which, since the extract-then-cap fix, can read it.
    """
    response = httpx.post(
        "https://api.tavily.com/search",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"query": query, "max_results": max_results},
        timeout=timeout_s,
    )
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    return [
        (
            str(row.get("title") or ""),
            str(row.get("url") or ""),
            str(row.get("content") or row.get("raw_content") or ""),
        )
        for row in rows
        if isinstance(row, dict)
    ]


class _SearchBackend(Protocol):
    """Uniform call shape every `web_search` backend is adapted to."""

    def __call__(
        self, query: str, max_results: int, *, timeout_s: float,
    ) -> list[tuple[str, str, str]]: ...


def _resolve_search_backend(
    provider: str, api_key: str | None,
) -> tuple[_SearchBackend, str]:
    """Pick the `web_search` backend, returning `(backend, name_actually_used)`.

    A keyed provider named without its key does NOT fail boot — it warns
    and degrades to `ddgs`, because losing web search entirely is worse
    than losing result quality. The degrade is never silent: it is
    logged here AND the provider actually used is reported in every
    `web_search` result payload, so the event log shows which backend
    answered rather than leaving it to be inferred.
    """
    normalized = provider.strip().lower()
    keyed: Mapping[str, _KeyedSearchBackend] = {
        "exa": _exa_search_backend,
        "tavily": _tavily_search_backend,
    }
    if normalized in keyed:
        if not api_key:
            LOGGER.warning(
                "tools.web.search_provider=%r but its search_api_key_env names an "
                "unset/empty variable — degrading to the ddgs backend",
                normalized,
            )
            return _call_ddgs, "ddgs"
        return _with_api_key(keyed[normalized], api_key), normalized
    if normalized != "ddgs":
        LOGGER.warning(
            "unknown tools.web.search_provider=%r — using the ddgs backend",
            provider,
        )
    return _call_ddgs, "ddgs"


def _call_ddgs(
    query: str, max_results: int, *, timeout_s: float,
) -> list[tuple[str, str, str]]:
    """Adapt `_ddgs_search_backend` to `_SearchBackend`.

    Deliberately a late-bound module-global lookup rather than a
    captured function object: `_ddgs_search_backend`'s own docstring
    promises the acceptance script can monkeypatch that NAME, and
    capturing it at registry-build time would quietly break that seam.
    """
    return _ddgs_search_backend(query, max_results, timeout_s=timeout_s)


def _with_api_key(backend: _KeyedSearchBackend, api_key: str) -> _SearchBackend:
    """Close a keyed backend over its credential, yielding a `_SearchBackend`."""

    def _call(
        query: str, max_results: int, *, timeout_s: float,
    ) -> list[tuple[str, str, str]]:
        return backend(query, max_results, timeout_s=timeout_s, api_key=api_key)

    return _call


def _make_web_search(
    *,
    default_max_results: int,
    timeout_s: float,
    provider: str = DEFAULT_WEB_SEARCH_PROVIDER,
    api_key: str | None = None,
) -> Tool:
    """Bind `tools.web.{search_max_results,timeout_s,search_provider}` into `web_search`."""
    backend, provider_used = _resolve_search_backend(provider, api_key)

    def web_search(args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            msg = f"web_search: query must be a non-empty string (got {query!r})"
            raise ToolError(msg, code="invalid_argument")
        max_results = default_max_results
        max_results_arg = args.get("max_results")
        if (
            isinstance(max_results_arg, (int, float))
            and not isinstance(max_results_arg, bool)
            and math.isfinite(max_results_arg)
        ):
            # `json.loads` accepts Infinity/NaN on the tool-arg path and `int(inf)`
            # raises; a malformed value means "use the default" (ADR-0011 §12).
            max_results = int(max_results_arg)
        max_results = max(1, min(max_results, _WEB_SEARCH_MAX_RESULTS_CAP))
        try:
            rows = backend(query, max_results, timeout_s=timeout_s)
        except Exception as exc:
            # ddgs anti-bot churn and keyed 401/429 are routine (ADR-0011 §5):
            # name the backend, no in-handler retry.
            msg = f"web_search: {provider_used} backend error: {exc}"
            raise ToolError(msg, code="web_search_backend_error") from exc
        # Naming the backend keeps the `_resolve_search_backend` degrade visible
        # in the event log instead of inferable.
        return {
            "results": [
                f"{i}. {title} — {url} — {_collapse_ws(text)[:_WEB_SEARCH_RESULT_TEXT_CAP]}"
                for i, (title, url, text) in enumerate(rows, start=1)
            ],
            "provider": provider_used,
        }

    return Tool(
        name="web_search",
        description=(
            "Search the web and return numbered title — url — snippet rows. "
            "Use for questions about current events or anything not already known."
        ),
        input_schema=_WEB_SEARCH_INPUT_SCHEMA,
        handler=web_search,
        allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
        risk_level="L1",
        read_only=True,
    )


# --- web_fetch -----------------------------------------------------------------

DEFAULT_WEB_FETCH_MAX_BYTES: Final[int] = 2 * 1024 * 1024
"""Default `tools.web.fetch_max_bytes` (ADR-0011 D7) — how many bytes of
the RESPONSE BODY are drained off the socket.

This is a memory/DoS bound, NOT the output cap — that is
:data:`DEFAULT_WEB_FETCH_MAX_TEXT_BYTES`, applied to the extracted text
further downstream.

It was 8 KiB ("same order as every other D5 tool's output cap"), which
conflated the two: the drain stops at this many bytes of *raw markup*,
and a real page spends far more than 8 KiB on `<head>` (inline CSS,
JSON-LD, script tags) before the first byte of visible text. A 150 KB
page therefore reached the parser as pure `<head>` and extracted to
nothing at all — the tool reported "cut off before any body text was
parsed" on ordinary sites, i.e. it could not read the web. 2 MiB clears
the head of essentially any real document while still refusing an
unbounded stream."""

DEFAULT_WEB_FETCH_MAX_TEXT_BYTES: Final[int] = 8192
"""Default `tools.web.fetch_max_text_bytes` — the cap on the text this
tool actually returns, applied AFTER HTML extraction. Since ADR 0019 it
is the tool's ``max_result_chars``: the dispatcher counts characters over
the whole serialized result and windows the longest string head+tail.

This is the 8 KiB context bound the old `fetch_max_bytes` was really
reaching for. Capping extracted text (rather than raw bytes) is what
makes the number mean what it says: 8 KiB of readable prose, not 8 KiB
of markup that may contain none."""

_WEB_FETCH_HEADERS: Final[Mapping[str, str]] = {
    # httpx's default UA (`python-httpx/x.y.z`) is refused outright by a
    # meaningful slice of the web. Wikipedia answers it with a bare 403
    # whose body reads "Please set a user-agent and respect our robot
    # policy" — which reached the LLM as an unexplained failure on an
    # ordinary encyclopedia lookup.
    #
    # Self-identifying (a real name plus a contact URL) is what the
    # sites asking for a UA actually want, and it keeps this honest:
    # `web_fetch` does not pretend to be a browser, and pairing a
    # browser UA with a client that runs no JS earns bot-detection
    # blocks rather than avoiding them.
    "User-Agent": "Jarvis/1.0 (+https://github.com/alllllenshi/jarvis)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.5",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
"""Request headers for every `web_fetch` hop (see the UA note above)."""

_WEB_FETCH_MAX_REDIRECTS: Final[int] = 3
"""Hard ceiling on redirects FOLLOWED (ADR-0011 D5: "at most 3
redirects") — not configurable. A 4th redirect response is refused
rather than followed; up to `_WEB_FETCH_MAX_REDIRECTS` + 1 HTTP
requests are made in total (the initial request plus each followed
redirect)."""

_WEB_FETCH_INPUT_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "description": "The http(s) URL to fetch.",
        },
    },
    "required": ["url"],
}

_REDIRECT_STATUS_CODES: Final[frozenset[int]] = frozenset({301, 302, 303, 307, 308})


def _parse_content_length(raw: str | None) -> int | None:
    """Parse a `Content-Length` header value, or `None` if absent/junk."""
    if raw is None:
        return None
    try:
        value = int(raw.strip())
    except ValueError:
        return None
    return value if value >= 0 else None


@dataclass(frozen=True)
class _HopResponse:
    """One non-redirect-following HTTP response (ADR-0011 D5 `web_fetch`)."""

    status_code: int
    location: str | None
    content_type: str | None
    body: bytes
    total_bytes: int
    total_bytes_exact: bool = True
    """False iff `total_bytes` is a lower bound, not a measured total —
    the drain stopped at `max_bytes` (or the fetch deadline) on a
    response with no usable `Content-Length` (MUST-FIX 3, ADR-0011
    §12). `body` itself is always the true prefix actually read,
    capped at `max_bytes` regardless of this flag."""


class _FetchDeadlineExceededError(Exception):
    """Raised when the overall wall-clock fetch budget elapses mid-drain.

    MUST-FIX 3, ADR-0011 §12. Raised by `_http_get_one_hop`, caught by
    `_fetch_url_backend` — never escapes this module.
    """


def _http_get_one_hop(
    url: str, *, timeout_s: float, max_bytes: int, deadline: float,
) -> _HopResponse:
    """Issue ONE GET with redirects disabled, streamed, body capped at `max_bytes`.

    The caller (`_fetch_url_backend`) owns redirect-following — this
    function never does, so every hop's `Location` can be re-validated
    by the SSRF guard before it is followed (ADR-0011 D5 point 5).

    The drain STOPS as soon as `max_bytes` is collected (MUST-FIX 3,
    ADR-0011 §12) — `httpx`'s `timeout` is a per-read timeout, not a
    wall-clock budget, so draining the FULL body (as this used to do,
    to report an exact `total_bytes`) let a slow/adversarial server
    hold the connection open indefinitely while trickling data, and let
    a huge body cost full transfer time to deliver a capped prefix.
    `deadline` (an absolute `time.monotonic()` value, shared across
    every hop by `_fetch_url_backend`) is also checked every chunk, so
    a slow-loris server that never triggers `httpx`'s own read timeout
    still gets cut off.

    `total_bytes`/`total_bytes_exact`: when the server sends a
    `Content-Length`, that is trusted as the exact total (matches what
    every HTTP client does). Otherwise, if the drain reached the cap or
    deadline before the stream ended, `total_bytes` is only the number
    of bytes actually read off the wire — a true LOWER BOUND, reported
    as such via `total_bytes_exact=False` — never a claimed exact count
    for data that was never measured.

    This is the one seam the acceptance script monkeypatches
    (`tools._http_get_one_hop`) to exercise `_fetch_url_backend`'s
    redirect-walking + guard-revalidation + truncation logic without a
    real socket — same "substitute a seam" shape Step 5 used for
    `vault_root`.
    """
    with (
        httpx.Client(timeout=timeout_s, follow_redirects=False, trust_env=False) as client,
        client.stream("GET", url, headers=_WEB_FETCH_HEADERS) as response,
    ):
        content_type = response.headers.get("content-type")
        location = response.headers.get("location")
        declared_length = _parse_content_length(response.headers.get("content-length"))
        chunks: list[bytes] = []
        collected = 0
        stopped_early = False
        for chunk in response.iter_bytes():
            if collected < max_bytes:
                chunks.append(chunk[: max_bytes - collected])
            collected += len(chunk)
            if collected >= max_bytes:
                stopped_early = True
                break
            if time.monotonic() >= deadline:
                # Distinct from the `max_bytes` cap above: reaching the
                # cap is a normal, expected truncation of a large body;
                # running out of wall-clock time mid-drain means the
                # server is too slow (slow-loris) — surfaced as an
                # error by `_fetch_url_backend`, not a truncated
                # success.
                msg = f"exceeded the fetch deadline after {collected} bytes"
                raise _FetchDeadlineExceededError(msg)
        body = b"".join(chunks)
        if declared_length is not None:
            total_bytes, total_bytes_exact = declared_length, True
        elif stopped_early:
            total_bytes, total_bytes_exact = collected, False
        else:
            total_bytes, total_bytes_exact = collected, True
        return _HopResponse(
            status_code=response.status_code,
            location=location,
            content_type=content_type,
            body=body,
            total_bytes=total_bytes,
            total_bytes_exact=total_bytes_exact,
        )


@dataclass(frozen=True)
class _FetchOutcome:
    """Result of `_fetch_url_backend` — success fields XOR error fields."""

    ok: bool
    status_code: int = 0
    content_type: str | None = None
    body: bytes = b""
    total_bytes: int = 0
    total_bytes_exact: bool = True
    final_url: str = ""
    error_code: str | None = None
    error_message: str | None = None


def _fetch_url_backend(  # noqa: PLR0911 — one linear guard/canonicalize/fetch/redirect loop; each return is a distinct named terminal outcome (`_FetchOutcome.error_code`), same rationale as the other D5 handlers' `_emit_tool_error` call sites — splitting them apart would scatter the outcomes from the loop that produces them.
    url: str,
    *,
    timeout_s: float,
    max_bytes: int,
    max_redirects: int = _WEB_FETCH_MAX_REDIRECTS,
    resolver: Callable[
        [str], Sequence[ipaddress.IPv4Address | ipaddress.IPv6Address]
    ]
    | None = None,
) -> _FetchOutcome:
    """Guarded GET with manual redirect walking (ADR-0011 D5).

    Validates EVERY hop — the initial `url` and each `Location` header —
    through `validate_egress_url` before issuing that hop's request,
    and always fetches `_normalize_egress_url`'s canonical form of that
    hop, never the raw `Location`/input string (ADR-0011 §12 MUST-FIX
    1/2 root cause — the validated string must be the used string).
    This is what stops the classic bypass: a public URL that 302s to
    `http://127.0.0.1:8006/`. A guard refusal at any hop, and exceeding
    `max_redirects`, both return `ok=False` with a named `error_code`;
    neither ever raises past this function.

    A single wall-clock `deadline` (MUST-FIX 3, ADR-0011 §12) is set
    ONCE from `timeout_s` and shared across every hop, including the
    guard/canonicalization work between hops — no redirect chain can
    make one `web_fetch` call run longer than `timeout_s` total,
    contrary to the previous per-hop-only `httpx` timeout.

    `resolver` defaults to `None`, resolved to the module-level
    `_resolve_hostname_ips` INSIDE the function body rather than as a
    literal default value — a default value is bound once at `def`
    time, so monkeypatching `tools._resolve_hostname_ips` afterward
    (the acceptance script's hermetic seam for every caller that does
    not pass `resolver` explicitly, e.g. `web_fetch`'s registered
    handler) would silently have no effect against a frozen default.
    """
    resolve = resolver if resolver is not None else _resolve_hostname_ips
    deadline = time.monotonic() + timeout_s
    current = url
    hops_followed = 0
    while True:
        allowed, reason = validate_egress_url(current, resolver=resolve)
        if not allowed:
            return _FetchOutcome(
                ok=False,
                error_code="ssrf_guard_refused",
                error_message=f"web_fetch egress guard refused {current!r}: {reason}",
            )

        normalized = _normalize_egress_url(current)
        if normalized is None:
            # Should be unreachable — `validate_egress_url` just
            # allowed `current` via the identical parser — but refuse
            # rather than ever fall back to the raw string.
            return _FetchOutcome(
                ok=False,
                error_code="ssrf_guard_refused",
                error_message=f"web_fetch: {current!r} passed the guard but would not canonicalize",
            )

        remaining_s = deadline - time.monotonic()
        if remaining_s <= 0:
            return _FetchOutcome(
                ok=False,
                error_code="deadline_exceeded",
                error_message=(
                    f"web_fetch: exceeded the {timeout_s:g}s wall-clock budget "
                    f"before hop {hops_followed + 1}"
                ),
            )

        try:
            hop = _http_get_one_hop(
                normalized, timeout_s=remaining_s, max_bytes=max_bytes, deadline=deadline,
            )
        except _FetchDeadlineExceededError:
            return _FetchOutcome(
                ok=False,
                error_code="deadline_exceeded",
                error_message=f"web_fetch: exceeded the {timeout_s:g}s wall-clock budget",
            )
        except httpx.HTTPError as exc:
            return _FetchOutcome(
                ok=False,
                error_code="network_error",
                error_message=f"web_fetch: {exc}",
            )

        if hop.status_code in _REDIRECT_STATUS_CODES:
            if hop.location is None:
                return _FetchOutcome(
                    ok=False,
                    error_code="network_error",
                    error_message=f"web_fetch: {normalized!r} redirected with no Location header",
                )
            if hops_followed >= max_redirects:
                return _FetchOutcome(
                    ok=False,
                    error_code="too_many_redirects",
                    error_message=(
                        f"web_fetch: exceeded {max_redirects} redirects starting from {url!r}"
                    ),
                )
            hops_followed += 1
            current = urljoin(normalized, hop.location)
            continue

        return _FetchOutcome(
            ok=True,
            status_code=hop.status_code,
            content_type=hop.content_type,
            body=hop.body,
            total_bytes=hop.total_bytes,
            total_bytes_exact=hop.total_bytes_exact,
            final_url=normalized,
        )


class _ReadableHTMLParser(HTMLParser):
    """Strip an HTML document to its page title + tag-stripped visible text.

    stdlib-only (ADR-0011 D5: no bs4/lxml — `html.parser` is fine).
    `<script>`/`<style>` contents are dropped entirely; every other
    tag's text is kept, collapsed to single-space-joined chunks. Feeding
    a byte-capped, possibly mid-tag-truncated document is safe —
    `HTMLParser` is deliberately lenient about malformed/incomplete
    markup, and an unclosed tag at EOF simply stops emitting text there.
    """

    def __init__(self) -> None:
        """Start with empty title/text buffers and zero script/style depth."""
        super().__init__(convert_charrefs=True)
        self._title_parts: list[str] = []
        self._text_parts: list[str] = []
        self._in_title = False
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:  # noqa: ARG002 — HTMLParser calls this positionally; attrs is unused but must stay in the override's signature.
        """Track entry into `<title>` and `<script>`/`<style>` regions."""
        if tag in ("script", "style"):
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        """Track exit from `<title>` and `<script>`/`<style>` regions."""
        if tag in ("script", "style"):
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        """Collect text data outside of skipped regions."""
        if self._skip_depth:
            return
        if self._in_title:
            self._title_parts.append(data)
            return
        stripped = data.strip()
        if stripped:
            self._text_parts.append(stripped)

    @property
    def title(self) -> str:
        """Return the concatenated `<title>` text, stripped."""
        return "".join(self._title_parts).strip()

    @property
    def text(self) -> str:
        """Return the space-joined visible text chunks."""
        return " ".join(self._text_parts)


def _extract_readable_html(html_text: str) -> tuple[str, str]:
    """Return `(title, stripped_text)` from an HTML document (stdlib-only)."""
    parser = _ReadableHTMLParser()
    parser.feed(html_text)
    return parser.title, parser.text


def _parse_content_type_charset(content_type: str | None) -> str | None:
    """Extract a `charset=` parameter from a `Content-Type` header value."""
    if not content_type:
        return None
    for part in content_type.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.strip().lower() == "charset":
            return value.strip().strip('"').strip("'") or None
    return None


def _decode_hop_body(
    body: bytes, total_bytes: int, content_type: str | None,
) -> tuple[str, int, bool]:
    """`(text, undelivered_bytes, lossy)` honoring a declared `charset=`.

    SHOULD-FIX 7, ADR-0011 §12 — a `charset=gb2312` page used to decode
    to 100% U+FFFD because everything went through the UTF-8-only
    `truncate_utf8` path regardless of what `Content-Type` declared.

    UTF-8 (declared, or no usable charset named) still goes through
    `truncate_utf8` UNCHANGED — its codepoint-safe boundary handling is
    never duplicated here; this only adds ONE more decode attempt in
    front of it for a charset Python actually knows (stdlib `codecs`,
    no new dependency). A body cut mid-character by the caller's own
    `max_bytes` cap decodes with `errors="replace"` and is reported
    `lossy=True`, same signal `truncate_utf8` gives its own lossy case.
    """
    charset = _parse_content_type_charset(content_type)
    if charset is None or charset.lower().replace("_", "-") in ("utf-8", "utf8"):
        return truncate_utf8(body, total_bytes)
    try:
        codec_name = codecs.lookup(charset).name
    except LookupError:
        return truncate_utf8(body, total_bytes)
    undelivered = max(0, total_bytes - len(body))
    try:
        return body.decode(codec_name), undelivered, False
    except UnicodeDecodeError:
        return body.decode(codec_name, errors="replace"), undelivered, True
    except LookupError:
        # A declared charset can name a REGISTERED but non-text codec
        # (e.g. `charset=base64`) — `codecs.lookup` accepts it but
        # `bytes.decode` refuses it with `LookupError`, not
        # `UnicodeDecodeError`. Caught here (not left to MUST-FIX 2's
        # handler-boundary catch-all) so an unusual header degrades to
        # the same lossy-UTF-8 fallback as an unknown charset, rather
        # than a generic "unexpected error" observation.
        return truncate_utf8(body, total_bytes)


def _looks_like_html(body: bytes) -> bool:
    """Sniff a leading `<`/`<!doctype` to detect an HTML document.

    SHOULD-FIX 7, ADR-0011 §12 — used only when `Content-Type` is
    absent, so a header-less HTML response still gets routed through
    the tag-stripping path instead of being dumped as raw markup under
    the "plain text" branch.
    """
    stripped = body.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return stripped.startswith(b"<")


_WEB_EXTRACT_URL: Final[str] = "https://api.tavily.com/extract"


def _tavily_extract(url: str, *, api_key: str, timeout_s: float) -> dict[str, Any]:
    """Tavily `/extract` (basic depth) for one URL: markdown text, PDFs included.

    Raises `ToolError` when Tavily answered but could not extract the page
    (its `failed_results` reason) and `httpx.HTTPError` when Tavily itself
    failed; `web_fetch` falls back to fetching here on either.
    """
    response = httpx.post(
        _WEB_EXTRACT_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={"urls": [url], "extract_depth": "basic"},
        timeout=timeout_s,
    )
    response.raise_for_status()
    data = response.json()
    results = data.get("results") or []
    if not results:
        failed = (data.get("failed_results") or [{}])[0]
        msg = f"web_fetch: tavily could not extract {url}: {failed.get('error', 'unknown')}"
        raise ToolError(msg, code="web_fetch_failed")
    return {
        "url": results[0].get("url") or url,
        "content": results[0].get("raw_content") or "",
        "source": "tavily",
    }


def _html_content(html_text: str, *, body_truncated: bool) -> str:
    """The extracted text, or the one-line reason there is none."""
    if html_text.strip():
        return html_text
    if body_truncated:
        return (
            "(the page was cut off by the download cap before any body text "
            "was parsed — cannot tell whether it has visible content past the cut)"
        )
    # ADR-0011 D5: no JS rendering — an SPA that hydrates via script returns
    # only its shell here. Declared limitation, not an empty-page bug.
    return (
        "(no visible text in the initial HTML — this may be a "
        "JavaScript-rendered page; this tool does not execute JS)"
    )


def _self_fetch(url: str, *, timeout_s: float, max_bytes: int) -> dict[str, Any]:
    """Fetch and tag-strip one page here: the keyless path and Tavily's fallback (ADR-0011 D5)."""
    outcome = _fetch_url_backend(url, timeout_s=timeout_s, max_bytes=max_bytes)
    if not outcome.ok:
        raise ToolError(
            outcome.error_message or "web_fetch: unknown failure",
            code=outcome.error_code or "web_fetch_failed",
        )
    mime = (outcome.content_type or "").split(";", 1)[0].strip().lower()
    if not mime and _looks_like_html(outcome.body):
        # No Content-Type at all: sniff, or raw markup would pass as plain text.
        mime = "text/html"
    payload: dict[str, Any] = {
        "url": outcome.final_url,
        "status_code": outcome.status_code,
        "content_type": outcome.content_type,
        "source": "fetch",
    }
    if mime and not mime.startswith("text/"):
        payload["note"] = (
            f"non-text content-type {outcome.content_type!r}; body not decoded as text"
        )
        return payload
    text, undelivered_bytes, lossy = _decode_hop_body(
        outcome.body, outcome.total_bytes, outcome.content_type,
    )
    body_truncated = undelivered_bytes > 0
    if mime == "text/html":
        title, html_text = _extract_readable_html(text)
        if title:
            payload["title"] = title
        payload["content"] = _html_content(html_text, body_truncated=body_truncated)
    else:
        payload["content"] = text
    payload["total_bytes"] = outcome.total_bytes
    if body_truncated:
        # The 2 MiB drain cap fired; the text cap is the dispatcher's.
        payload["download_truncated"] = True
    if not outcome.total_bytes_exact:
        payload["total_bytes_exact"] = False
    if lossy:
        payload["encoding"] = "lossy"
    return payload


def _make_web_fetch(
    *, max_bytes: int, max_text_chars: int, timeout_s: float, extract_api_key: str | None,
) -> Tool:
    """Bind the `tools.web.*` fetch knobs and the Tavily key into `web_fetch`.

    With a key the page comes from Tavily extract (markdown, PDFs, no JS
    limitation); any Tavily failure falls back to fetching here. The egress
    guard runs first either way, so no private address leaves the machine.
    """

    def web_fetch(args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        url = args.get("url")
        if not isinstance(url, str) or not url.strip():
            msg = f"web_fetch: url must be a non-empty string (got {url!r})"
            raise ToolError(msg, code="invalid_argument")
        allowed, reason = validate_egress_url(url)
        if not allowed:
            msg = f"web_fetch: {reason}"
            raise ToolError(msg, code="ssrf_guard_refused")
        if extract_api_key:
            try:
                return _tavily_extract(url, api_key=extract_api_key, timeout_s=timeout_s)
            except (httpx.HTTPError, ToolError, ValueError) as exc:
                LOGGER.warning(
                    "web_fetch: tavily extract failed for %s (%s); fetching directly", url, exc,
                )
        return _self_fetch(url, timeout_s=timeout_s, max_bytes=max_bytes)

    return Tool(
        name="web_fetch",
        description=(
            "Fetch a URL's readable content: markdown (PDFs included) via Tavily "
            "extract when configured, else the page title plus tag-stripped text. "
            "The direct path does not execute JavaScript."
        ),
        input_schema=_WEB_FETCH_INPUT_SCHEMA,
        handler=web_fetch,
        allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
        risk_level="L1",
        read_only=True,
        max_result_chars=max_text_chars,
    )


# --- open_url --------------------------------------------------------------

_OPEN_URL_SUBPROCESS_TIMEOUT_S: Final[float] = 10.0

_OPEN_URL_INPUT_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "description": "The http(s) URL to open in the default browser.",
        },
    },
    "required": ["url"],
}


def open_url_handler(  # noqa: PLR0911 — one linear validate/canonicalize/subprocess/ack pass; each return is a distinct named terminal outcome via `_emit_tool_error`/`_emit_tool_observation`, same rationale as the D5 handlers this mirrors.
    action_request: ActionRequest,
    conn: sqlite3.Connection,
    runtime_paths: RuntimePathsLike,  # noqa: ARG001 — signature uniformity.
    lifecycle: ActionLifecycle,
) -> RawResult:
    """Open a URL in the default browser (ADR-0011 D5).

    Same scheme + address checks as `web_fetch` via `validate_egress_url`
    — `open http://127.0.0.1:8006/shutdown` would otherwise open Allen's
    browser against his own daemon. `open` is handed
    `_normalize_egress_url`'s canonical re-serialization of `url`, never
    `url` itself (ADR-0011 §12 MUST-FIX 1/2 root cause) — the browser's
    WHATWG URL parser reads some strings (a backslash before the last
    `@` in the authority, in particular) differently from the
    `urlsplit`-based guard that just validated them; canonicalizing
    strips exactly the userinfo/fragment components that trick relies
    on. Result is an ACK (`result_semantics="ack"`, Execution Claim
    `executed`) — the browser actually opening is not verified, same
    posture `open_path_handler` documents for its own `open` call.
    """
    running_event_uid = _get_running_event_uid(conn, action_request.action_id)
    action_id = action_request.action_id

    try:
        url = action_request.arguments.get("url")
        if not isinstance(url, str) or not url.strip():
            return _emit_tool_error(
                conn=conn,
                lifecycle=lifecycle,
                action_id=action_id,
                running_event_uid=running_event_uid,
                code="invalid_argument",
                message=f"open_url: url must be a non-empty string (got {url!r})",
            )

        allowed, reason = validate_egress_url(url)
        if not allowed:
            return _emit_tool_error(
                conn=conn,
                lifecycle=lifecycle,
                action_id=action_id,
                running_event_uid=running_event_uid,
                code="ssrf_guard_refused",
                message=f"open_url egress guard refused {url!r}: {reason}",
            )

        normalized = _normalize_egress_url(url)
        if normalized is None:
            # Should be unreachable — the guard just allowed `url` via
            # the identical parser — but refuse rather than ever fall
            # back to the raw string.
            return _emit_tool_error(
                conn=conn,
                lifecycle=lifecycle,
                action_id=action_id,
                running_event_uid=running_event_uid,
                code="ssrf_guard_refused",
                message=f"open_url: {url!r} passed the guard but would not canonicalize",
            )

        try:
            # argv list, no shell; `normalized` already passed the
            # scheme+address allowlist above; `open` resolved via PATH
            # matches the `pbpaste`/`open_path` precedent.
            proc = subprocess.run(  # noqa: S603
                ["open", normalized],  # noqa: S607
                timeout=_OPEN_URL_SUBPROCESS_TIMEOUT_S,
                capture_output=True,
                text=True,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return _emit_tool_error(
                conn=conn,
                lifecycle=lifecycle,
                action_id=action_id,
                running_event_uid=running_event_uid,
                code="open_failed",
                message=f"open_url: subprocess failed to start: {exc}",
            )

        if proc.returncode != 0:
            stderr_tail = (proc.stderr or "").strip()[-_OUTPUT_TAIL_BYTES:]
            return _emit_tool_error(
                conn=conn,
                lifecycle=lifecycle,
                action_id=action_id,
                running_event_uid=running_event_uid,
                code="open_failed",
                message=f"open_url: `open` exited {proc.returncode}: {stderr_tail}",
            )

        payload: dict[str, Any] = {"url": normalized}
        return _emit_tool_observation(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_id,
            running_event_uid=running_event_uid,
            payload=payload,
            semantics="ack",
        )
    except Exception as exc:  # noqa: BLE001 — handler-boundary catch-all (MUST-FIX 2, ADR-0011 §12), same rationale as `web_fetch`'s: §5's contract is "never a crash", so any unexpected failure still terminal-transitions the lifecycle, naming the real exception type, instead of stranding it at `running`.
        return _emit_tool_error(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_id,
            running_event_uid=running_event_uid,
            code="open_url_unexpected_error",
            message=f"open_url: unexpected {type(exc).__name__}: {exc}",
        )


# --- screen_look ---------------------------------------------------------------


class VisionClient(Protocol):
    """Injected one-shot vision seam for `screen_look` (ADR-0011 D5/D7).

    L4 cannot import `jarvis.decision.llm.LLMClient` — sibling layers
    under `.importlinter`. `jarvis.runtime` (the composition root)
    builds the real implementation around the `llm.presets.vision`
    preset and binds it into `build_default_registry` at
    registry-build time — the same seam Step 5/6 used for
    `vault_root` / `tools.web.*`.

    One call: an image path (+ optional question) in, text out. The
    handler hands this a `Path`, never bytes — reading the file and
    base64-encoding it for the actual vision request happens entirely
    inside the injected implementation, so no image data ever passes
    through, or is held by, this L4 module (spec §3.3.9 / ADR-0011 §3
    D5: the payload carries the artifact path, never image bytes).
    Any failure must be raised, not swallowed — the handler's own
    boundary catch turns it into an error observation; the screenshot
    artifact already on disk is unaffected by a vision failure
    (ADR-0011 §5).
    """

    def describe_image(self, image_path: Path, *, question: str | None) -> str:
        """Return a text description of the image at `image_path`."""
        ...


DEFAULT_SCREEN_MAX_WIDTH_PX: Final[int] = 1568
"""Default `tools.screen.max_width_px` (ADR-0011 D7) — the vision
model's own recommended upper bound on input image width."""

_SCREEN_ARTIFACTS_DIRNAME: Final[str] = "screen_artifacts"
"""Subdirectory of `runtime_paths.artifacts_root`, mirroring the
`voice_artifacts/` precedent (`jarvis/runtime/inherent_loop.py:735`).
Nothing prunes it — same as `voice_artifacts/`, which has no pruner
either (`jarvis/surface/voice_artifact_store.py`); screenshots
accumulate on disk indefinitely."""

_SCREEN_CAPTURE_TIMEOUT_S: Final[float] = 10.0
_SIPS_TIMEOUT_S: Final[float] = 10.0

_PNG_MAGIC: Final[bytes] = b"\x89PNG\r\n\x1a\n"


def _looks_like_png(path: Path) -> bool:
    """Check the real 8-byte PNG magic (NIT-FIX 6, ADR-0011 §12).

    A bare `st_size == 0` check lets a truncated write through — e.g.
    a 3-byte fragment from an interrupted `screencapture` is non-empty
    and would otherwise reach the vision model (and get billed for
    garbage input) instead of being rejected here. Stdlib only, no new
    imaging dependency.
    """
    try:
        with path.open("rb") as fh:
            return fh.read(len(_PNG_MAGIC)) == _PNG_MAGIC
    except OSError:
        return False


_SCREEN_LOOK_INPUT_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": (
                "Optional: focus the screen description on this question "
                "instead of a generic summary."
            ),
        },
    },
    "required": [],
}


def _run_screencapture(
    output_path: Path, *, timeout_s: float,
) -> subprocess.CompletedProcess[bytes]:
    """Real `screencapture -x -m` invocation (ADR-0011 D5).

    Named seam, not inlined, so the acceptance script can substitute a
    fake without ever invoking the real macOS screenshot utility — the
    first real invocation triggers the Screen Recording TCC prompt,
    which needs Allen physically present to approve.

    `-m` restricts the capture to the main display (NIT-FIX 6, ADR-0011
    §12): without it, a multi-display Mac gets one file per display,
    and every file but the one at `output_path` is an orphan — no
    payload field ever references it and nothing prunes it. Declared
    limitation: on a multi-display Mac this tool only ever sees the
    main display.
    """
    return subprocess.run(  # noqa: S603
        ["screencapture", "-x", "-m", str(output_path)],  # noqa: S607 — resolved via PATH, matches pbpaste/open precedent.
        timeout=timeout_s,
        capture_output=True,
        text=False,
        check=False,
    )


def _run_sips_downscale(
    image_path: Path, max_width_px: int, *, timeout_s: float,
) -> subprocess.CompletedProcess[bytes]:
    """Real in-place `sips -Z <max_width_px>` downscale (ADR-0011 D5).

    `-Z` / `--resampleHeightWidthMax` bounds the LARGER of the two
    dimensions to at most `max_width_px` (preserving aspect ratio, and
    not upscaling an already-smaller image) — not width specifically.
    For an ordinary landscape screenshot the two readings coincide, so
    behavior matches intent; a rotated display would make height the
    binding dimension, and `-Z` still produces the correct bound in
    that case too (NIT-FIX 6, ADR-0011 §12 — `max_width_px` is simply a
    narrower name than what this actually controls). Named seam, same
    rationale as `_run_screencapture`.
    """
    return subprocess.run(  # noqa: S603
        ["sips", "-Z", str(max_width_px), str(image_path)],  # noqa: S607 — resolved via PATH.
        timeout=timeout_s,
        capture_output=True,
        text=False,
        check=False,
    )


def _screen_recording_permission_message() -> str:
    """ADR-0011 §5's TCC-denial sentence, with the real interpreter path."""
    return f"Screen Recording permission missing for {sys.executable} — System Settings → Privacy"


def _make_screen_look(*, vision_client: VisionClient | None, max_width_px: int) -> Tool:
    """Bind the injected vision client + `tools.screen.max_width_px` (ADR-0011 D7).

    `vision_client=None` means `jarvis.runtime` found no usable
    `llm.presets.vision` block: the tool still registers (the menu stays
    complete) but every call degrades to a `vision_unconfigured` error —
    AFTER the screenshot is captured and saved, so evidence survives a
    config gap the same way it survives a live proxy outage.
    """

    def screen_look(args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
        question_raw = args.get("question")
        question = (
            question_raw.strip() if isinstance(question_raw, str) and question_raw.strip() else None
        )
        artifacts_dir = ctx.runtime_paths.artifacts_root / _SCREEN_ARTIFACTS_DIRNAME
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        # `action_id` makes this collision-free; the timestamp prefix is for chronological `ls`.
        image_path = artifacts_dir / f"{int(time.time() * 1000)}_{ctx.action_id}.png"

        try:
            capture_proc = _run_screencapture(image_path, timeout_s=_SCREEN_CAPTURE_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as exc:
            msg = f"screen_look: screencapture failed to run: {exc}"
            raise ToolError(msg, code="screen_capture_start_failed") from exc
        if capture_proc.returncode != 0:
            # The cause of a non-zero exit is not determinable from here (TCC
            # denial, bad path, full disk...): report what happened and name
            # Screen Recording only as the likely first-run cause.
            stderr_tail = (capture_proc.stderr or b"").decode("utf-8", errors="replace").strip()
            msg = (
                f"screen_look: screencapture exited {capture_proc.returncode}"
                f"{f': {stderr_tail}' if stderr_tail else ''}. If this is the "
                f"first screen_look call, the likely cause is missing Screen "
                f"Recording permission for {sys.executable} — System Settings → "
                "Privacy; otherwise this is a genuine screencapture failure."
            )
            raise ToolError(msg, code="screen_capture_failed")
        if not image_path.is_file() or not _looks_like_png(image_path):
            # On some macOS versions a TCC-denied screencapture exits 0 but
            # writes nothing (or a truncated file): that shape IS specific
            # enough to name Screen Recording as the cause.
            msg = (
                f"screen_look: {_screen_recording_permission_message()} "
                "(screencapture produced no image data)"
            )
            raise ToolError(msg, code="screen_recording_permission_denied")

        try:
            sips_proc = _run_sips_downscale(image_path, max_width_px, timeout_s=_SIPS_TIMEOUT_S)
            if sips_proc.returncode != 0:
                LOGGER.warning(
                    "screen_look: sips downscale failed (exit %s) for %s; "
                    "continuing with the full-resolution screenshot",
                    sips_proc.returncode,
                    image_path,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            # Downscaling is a cost/latency optimization, not a correctness
            # requirement; the artifact on disk is untouched either way.
            LOGGER.warning(
                "screen_look: sips failed to run (%s) for %s; "
                "continuing with the full-resolution screenshot",
                exc,
                image_path,
            )

        if vision_client is None:
            msg = (
                "screen_look: no vision client configured "
                f"(llm.presets.vision missing or invalid); screenshot saved at {image_path}"
            )
            raise ToolError(msg, code="vision_unconfigured")
        try:
            description = vision_client.describe_image(image_path, question=question)
        except Exception as exc:  # degrades to an error; the screenshot is on disk regardless.
            msg = (
                f"screen_look: vision call failed: {type(exc).__name__}: {exc}; "
                f"screenshot saved at {image_path}"
            )
            raise ToolError(msg, code="vision_call_failed") from exc

        return {"artifact_path": str(image_path), "description": description}

    return Tool(
        name="screen_look",
        description=(
            "Take a screenshot of Allen's screen and describe what's on "
            "it via a vision model; an optional `question` focuses the "
            "description on something specific. The decision LLM never "
            "sees the screenshot pixels — only this tool's returned "
            "text description."
        ),
        input_schema=_SCREEN_LOOK_INPUT_SCHEMA,
        handler=screen_look,
        allowed_callers=frozenset({CallerPrincipal.REGEX_ROUTER, CallerPrincipal.JARVIS_LLM}),
        risk_level="L1",
        read_only=True,
    )


# --- ToolRegistry ------------------------------------------------------------


class ToolRegistry:
    """L4 dispatch surface (ADR § Module map L4 row).

    Owns:
        - The `name → ToolDefinition | Tool` table.
        - `for_caller(...)` — filtered list the L3 system-prompt assembler
          renders into the LLM tool list.
        - `dispatch(...)` — the L3-facing entry point. Validates caller,
          lifecycle precondition, emits `action.dispatched` +
          `action.running`, calls the handler, returns its `RawResult`.

    Owns NOT:
        - Lifecycle FSM mutation outside `authorized → dispatched → running`
          (a ``ToolDefinition`` handler writes its own terminal; a flat
          ``Tool`` gets the dispatcher's).

    Thread-safe by `threading.RLock` around the internal dict; same
    rationale as `ActionLifecycle`.
    """

    def __init__(self, *, confirmation_dispatch_outbox: bool = False) -> None:
        """Construct an empty registry (no tools yet)."""
        self._tools: dict[str, ToolDefinition | Tool] = {}
        self._lock = threading.RLock()
        self._confirmation_dispatch_outbox = confirmation_dispatch_outbox

    def register(self, tool_def: ToolDefinition | Tool) -> None:
        """Register a ToolDefinition. Raises DuplicateToolError on re-register.

        Unlike legacy `tools_v2/registry.py` (which logged and overwrote),
        Day-1 raises so a double-register due to a misconfigured
        composition root is caught at runtime.
        """
        with self._lock:
            if tool_def.name in self._tools:
                msg = f"tool {tool_def.name!r} is already registered"
                raise DuplicateToolError(msg)
            self._tools[tool_def.name] = tool_def

    def get_definitions(self) -> tuple[ToolDefinition | Tool, ...]:
        """Return every registered tool, in registration order."""
        with self._lock:
            return tuple(self._tools.values())

    def replace_group(self, previous: frozenset[str], tools: tuple[Tool, ...]) -> None:
        """Atomically publish a runtime-owned plugin group, preserving other tools."""
        with self._lock:
            names = [tool.name for tool in tools]
            if len(set(names)) != len(names) or (set(names) & (self._tools.keys() - previous)):
                msg = "plugin tools collide with another registered tool"
                raise DuplicateToolError(msg)
            self._tools = {
                **{name: tool for name, tool in self._tools.items() if name not in previous},
                **{tool.name: tool for tool in tools},
            }

    def for_caller(self, caller_principal: CallerPrincipal) -> tuple[ToolDefinition | Tool, ...]:
        """Return only the tools `caller_principal` is allowed to dispatch."""
        with self._lock:
            return tuple(
                t for t in self._tools.values() if caller_principal in t.allowed_callers
            )

    def dispatch(
        self,
        action_request: ActionRequest,
        conn: sqlite3.Connection,
        runtime_paths: RuntimePathsLike,
        lifecycle: ActionLifecycle,
    ) -> RawResultBundle:
        """Dispatch one ActionRequest and return its :class:`RawResultBundle`.

        Day-2 (ADR-0002 § RawResultBundle contract wrapping rule): the
        dispatcher uniformly returns ``RawResultBundle`` to L3; a bare
        :class:`RawResult` is wrapped in ``RawResultBundle(slots=(result,))``.

        Preconditions:
            - `action_request.tool_name` is registered → else `UnknownToolError`.
            - `action_request.caller_principal` is in `tool_def.allowed_callers`
              → else `CallerNotAllowedError`.
            - `lifecycle.state_of(action_id) == "authorized"` → else
              `IllegalLifecycleTransition` (the caller, L3, is expected
              to `register` the action and transition `proposed →
              authorized` before this call).

        On precondition pass, registers the action_id in the live set
        (ADR-0009 D4 — the supervisor sweep skips live action_ids;
        `jarvis.runtime.drive_turn` releases the turn's entries in a
        `finally`), then emits:
            1. `action.dispatched(action_id)` → lifecycle authorized →
               dispatched. The event_uid is the `source_event_id` of
               the next event.
            2. `action.running(action_id)` → lifecycle dispatched →
               running. The event_uid is stashed so the handler can use
               it as `source_event_id` for `action.result_observed`.

        The handler then runs on the calling thread and returns a
        `RawResult` or `RawResultBundle`.
        """
        tool_def = self._checked_tool_def(action_request, lifecycle)
        dispatched_event = self._emit_dispatched(action_request, conn, lifecycle)
        return self._run_inline(
            action_request,
            conn,
            runtime_paths,
            lifecycle,
            tool_def=tool_def,
            dispatched_event_uid=dispatched_event.event_uid,
        )

    def _checked_tool_def(
        self,
        action_request: ActionRequest,
        lifecycle: ActionLifecycle,
    ) -> ToolDefinition | Tool:
        """Validate the three dispatch preconditions and return the definition."""
        with self._lock:
            tool_def = self._tools.get(action_request.tool_name)
        if tool_def is None:
            msg = f"unknown tool: {action_request.tool_name!r}"
            raise UnknownToolError(msg)

        if action_request.caller_principal not in tool_def.allowed_callers:
            allowed_names = sorted(c.value for c in tool_def.allowed_callers)
            msg = (
                f"tool {tool_def.name!r} is not callable from "
                f"caller_principal={action_request.caller_principal.value!r}. "
                f"Allowed: {allowed_names!r}"
            )
            raise CallerNotAllowedError(msg)

        current = lifecycle.state_of(action_request.action_id)
        if current != "authorized":
            msg = (
                f"dispatch precondition not met for action_id="
                f"{action_request.action_id!r}: lifecycle must be 'authorized' "
                f"at dispatch (current={current!r})"
            )
            raise IllegalLifecycleTransition(msg)
        return tool_def

    def _emit_dispatched(
        self,
        action_request: ActionRequest,
        conn: sqlite3.Connection,
        lifecycle: ActionLifecycle,
    ) -> Event:
        """Register the action as live and commit its `action.dispatched`."""
        # ADR-0009 D4 — publish the action as live BEFORE the first event
        # lands, so a sweep tick that runs between the two never sees an
        # unprotected open action. `drive_turn` owns the release.
        if action_request.turn_id is not None:
            register_live_action(
                turn_id=action_request.turn_id,
                action_id=action_request.action_id,
            )

        dispatched_payload: dict[str, Any] = {"action_id": action_request.action_id}
        with action_admission_guard():
            if (
                self._confirmation_dispatch_outbox
                and action_request.authorization_lease is not None
            ):
                dispatched_event = admit_authorized_dispatch(
                    conn, action_request, payload=dispatched_payload,
                    correlation=_action_correlation(action_request),
                )
            else:
                dispatched_event = emit_event(
                    conn,
                    type="action.dispatched",
                    payload=dispatched_payload,
                    correlation=_action_correlation(action_request),
                )
            lifecycle.transition(action_request.action_id, "dispatched")
        return dispatched_event

    def _run_inline(  # noqa: PLR0913 — the four dispatch arguments plus the two values `dispatch` already computed.
        self,
        action_request: ActionRequest,
        conn: sqlite3.Connection,
        runtime_paths: RuntimePathsLike,
        lifecycle: ActionLifecycle,
        *,
        tool_def: ToolDefinition | Tool,
        dispatched_event_uid: str,
    ) -> RawResultBundle:
        """Run the handler on the calling thread."""
        running_event = emit_event(
            conn,
            type="action.running",
            payload={"action_id": action_request.action_id},
            source_event_id=dispatched_event_uid,
            correlation=_action_correlation(action_request),
        )
        lifecycle.transition(action_request.action_id, "running")

        # Stash the running event_uid so the handler can use it as the
        # source_event_id for action.result_observed.
        _set_running_event_uid(conn, action_request.action_id, running_event.event_uid)
        try:
            handler_result = _run_handler(
                tool_def, action_request, conn, runtime_paths, lifecycle,
                running_event_uid=running_event.event_uid,
            )
        finally:
            _clear_running_event_uid(conn, action_request.action_id)

        # § RawResultBundle wrapping rule: bare RawResult returns get
        # wrapped here so L3 always sees the bundle shape.
        if isinstance(handler_result, RawResultBundle):
            return handler_result
        return RawResultBundle(slots=(handler_result,))

# --- running_event_uid handoff (dispatcher → handler) -----------------------
#
# The handlers need to know the `event_uid` of the `action.running` event
# the dispatcher just emitted, so they can set it as `source_event_id` on
# the events they emit next (`action.result_observed`).
#
# We cannot stash this on `sqlite3.Connection` (it forbids arbitrary
# attribute assignment), and we cannot widen the ToolHandler signature
# (the public contract is fixed). Instead, the dispatcher records the
# uid in a module-level dict keyed by `action_id`, guarded by a Lock,
# and clears the entry in a `try/finally`. Per-`action_id` keying is
# safe because Day-1 each `action_id` is unique and only one dispatch
# is in flight per `action_id` at a time (the lifecycle FSM rejects
# re-dispatch).

_RUNNING_UID_LOCK: Final[threading.Lock] = threading.Lock()
_RUNNING_UID_TABLE: Final[dict[str, str]] = {}


def _set_running_event_uid(
    conn: sqlite3.Connection,  # noqa: ARG001 — `conn` kept for API symmetry / future per-conn keying.
    action_id: str,
    event_uid: str,
) -> None:
    """Stash the action.running event_uid for the handler to pick up."""
    with _RUNNING_UID_LOCK:
        _RUNNING_UID_TABLE[action_id] = event_uid


def _get_running_event_uid(
    conn: sqlite3.Connection,  # noqa: ARG001 — see `_set_running_event_uid`.
    action_id: str,
) -> str:
    """Retrieve the action.running event_uid stashed by the dispatcher."""
    with _RUNNING_UID_LOCK:
        if action_id not in _RUNNING_UID_TABLE:
            msg = (
                f"running_event_uid for action_id={action_id!r} was not stashed; "
                "handler must be called through ToolRegistry.dispatch"
            )
            raise IllegalLifecycleTransition(msg)
        return _RUNNING_UID_TABLE[action_id]


def _clear_running_event_uid(
    conn: sqlite3.Connection,  # noqa: ARG001 — see `_set_running_event_uid`.
    action_id: str,
) -> None:
    """Drop the stash entry once the handler returns."""
    with _RUNNING_UID_LOCK:
        _RUNNING_UID_TABLE.pop(action_id, None)


# --- Live action-id set (ADR-0009 D4 active-turn exclusion) ------------------
#
# The supervisor sweep (`jarvis.deployment.sleep_wake`) closes open
# actions whose deadline has passed. It must never close an action a turn
# is still driving, and it cannot ask the `ActionLifecycle` FSM: that is a
# per-process dict, empty in the sweep's own view after any restart.
#
# So the dispatch path publishes each action_id here, and the composition
# root (`jarvis.runtime.drive_turn`) drops the turn's whole entry in a
# `finally` — including when the turn raises, because an entry nobody
# removes pins its action_id "active" for the life of the process and the
# sweep could never close the very orphan that crash created.
#
# Keyed by turn_id (not a flat set) so one turn's release cannot unprotect
# a concurrent turn's actions — the daemon drives turns on worker threads.
# Storage sits in L4 because L4 is where dispatch happens and L4 may not
# import runtime; L6 reads it only through a value the composition root
# passes down, so `deployment -> execution` never appears in the graph.
# Same module-level + Lock shape as `_RUNNING_UID_TABLE` above.

_LIVE_ACTIONS_LOCK: Final[threading.Lock] = threading.Lock()
_LIVE_ACTIONS_BY_TURN: Final[dict[str, set[str]]] = {}


def register_live_action(*, turn_id: str, action_id: str) -> None:
    """Publish ``action_id`` as driven by ``turn_id`` (idempotent)."""
    with _LIVE_ACTIONS_LOCK:
        _LIVE_ACTIONS_BY_TURN.setdefault(turn_id, set()).add(action_id)


def release_turn_actions(turn_id: str) -> None:
    """Drop every action_id ``turn_id`` registered. No-op if unknown."""
    with _LIVE_ACTIONS_LOCK:
        _LIVE_ACTIONS_BY_TURN.pop(turn_id, None)


def live_action_ids() -> frozenset[str]:
    """Snapshot every action_id a live turn is driving."""
    with _LIVE_ACTIONS_LOCK:
        return frozenset(
            action_id
            for action_ids in _LIVE_ACTIONS_BY_TURN.values()
            for action_id in action_ids
        )


def turn_action_ids(turn_id: str) -> frozenset[str]:
    """Snapshot the action_ids ``turn_id`` alone owns (empty when unknown).

    The per-turn narrowing of :func:`live_action_ids`, read by the
    composition root's in-turn trigger waiter (ADR-0009 D4 / F9). The
    waiter must accept ONLY its own actions' terminal events: the global
    live set would still let one in-flight turn adopt a *concurrent*
    turn's timeout — the daemon drives turns on worker threads, so two
    can be live at once.
    """
    with _LIVE_ACTIONS_LOCK:
        return frozenset(_LIVE_ACTIONS_BY_TURN.get(turn_id, ()))


def _action_correlation(action_request: ActionRequest) -> dict[str, str]:
    """Build the canonical `{action_id, run_id?, turn_id?}` correlation mapping."""
    out: dict[str, str] = {"action_id": action_request.action_id}
    if action_request.run_id is not None:
        out["run_id"] = action_request.run_id
    if action_request.turn_id is not None:
        out["turn_id"] = action_request.turn_id
    return out


# --- write_file (ADR-0012 D1) ------------------------------------------------
#
# The only L3 tool: read_only=False, requires_entity=True,
# requires_confirmation=True. Dispatch reaches this handler only via a
# gated ActionRequest carrying a valid AuthorizationLease (ADR-0012 D2)
# — the Pre-action Gate's confirm_required/lease machinery is what makes
# that true, not anything in this handler.

_WRITE_FILE_ENTITY_PREFIX: Final[str] = "file:"

_WRITE_FILE_MODES: Final[frozenset[str]] = frozenset({"create", "overwrite", "append"})

_WRITE_FILE_MODE_TO_OPEN_MODE: Final[Mapping[str, str]] = {
    "create": "x",
    "overwrite": "w",
    "append": "a",
}

_WRITE_FILE_INPUT_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "target": {
            "type": "string",
            "description": (
                "Spoken name, description, or path of the file to write. "
                "Resolved to a canonical path before writing (never used as "
                "a raw path). An existing file resolves normally; a "
                "non-existent file resolves only if its parent directory is "
                "in scope."
            ),
        },
        "content": {
            "type": "string",
            "description": "Text content to write.",
        },
        "mode": {
            "type": "string",
            "description": (
                "'create' refuses if the file already exists; 'overwrite' "
                "and 'append' both require an existing file."
            ),
        },
    },
    "required": ["target", "content", "mode"],
}


def write_file_handler(  # noqa: PLR0911 — one linear resolve/validate/mode/write pass; each return is a distinct, named failure or the single success exit — splitting it would only relocate the branches, not reduce them.
    action_request: ActionRequest,
    conn: sqlite3.Connection,
    runtime_paths: RuntimePathsLike,  # noqa: ARG001 — signature uniformity.
    lifecycle: ActionLifecycle,
) -> RawResult:
    """L3 `write_file` — the confirmation flow's acceptance instrument (ADR-0012 D1).

    Writes to the RESOLVED canonical path off
    `action_request.target_entity_ref` — NEVER `arguments["target"]`,
    the raw free text the LLM supplied. Same discipline as
    `read_file_handler`: the Pre-action Gate has already required a
    trusted `"file:<abs-path>"` ref (ADR-0011 D3 + ADR-0012 D1's
    write-target resolution extension) by the time this handler runs,
    so the raw argument is never read here. `target_entity_ref is
    None` cannot pass the gate for a `requires_entity=True` tool, but
    this handler checks it anyway — a handler must be safe standing
    alone, not merely behind a gate that happens to always run first
    in production.

    Mode semantics: `create` refuses an already-existing file;
    `overwrite`/`append` both require an existing file. The existence
    check happens twice by design — once explicitly (for a clear,
    mode-specific error code) and once implicitly via the `"x"` open
    mode for `create` (atomic — closes the TOCTOU gap the explicit
    check alone would leave between check and write).

    Any I/O error (permission denied, disk full, parent directory
    vanished between resolve and dispatch, ...) becomes an error
    observation via `_emit_tool_error` and rides the EXISTING
    Limitation routing — ADR-0012 §4 failure-mode table: "write_file
    handler I/O error -> error observation -> Limitation routing
    (existing machinery)". No new error machinery is introduced here.

    Result semantics is `"ack"` (Execution Claim `executed` — the
    write-was-accepted, not content-verified; ADR-0012 D1 explicitly
    defers read-back `post_action_check` to a future ADR).
    """
    running_event_uid = _get_running_event_uid(conn, action_request.action_id)
    action_id = action_request.action_id

    ref = action_request.target_entity_ref
    if ref is None or not ref.startswith(_WRITE_FILE_ENTITY_PREFIX):
        return _emit_tool_error(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_id,
            running_event_uid=running_event_uid,
            code="no_resolved_target",
            message="write_file: no resolved target_entity_ref (the gate should have refused this)",
        )
    path = Path(ref[len(_WRITE_FILE_ENTITY_PREFIX) :])

    mode_raw = action_request.arguments.get("mode")
    mode = mode_raw if isinstance(mode_raw, str) else ""
    if mode not in _WRITE_FILE_MODES:
        return _emit_tool_error(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_id,
            running_event_uid=running_event_uid,
            code="invalid_mode",
            message=(
                f"write_file: mode must be one of {sorted(_WRITE_FILE_MODES)!r}, "
                f"got {mode_raw!r}"
            ),
        )

    content_raw = action_request.arguments.get("content")
    content = content_raw if isinstance(content_raw, str) else None
    if content is None:
        return _emit_tool_error(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_id,
            running_event_uid=running_event_uid,
            code="missing_content",
            message="write_file: 'content' argument must be a string",
        )

    exists = path.is_file()
    if mode == "create" and exists:
        return _emit_tool_error(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_id,
            running_event_uid=running_event_uid,
            code="target_exists",
            message=f"write_file: {path} already exists; mode=create refuses to overwrite it",
        )
    if mode in ("overwrite", "append") and not exists:
        return _emit_tool_error(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_id,
            running_event_uid=running_event_uid,
            code="target_not_found",
            message=f"write_file: {path} does not exist; mode={mode!r} requires an existing file",
        )

    try:
        with path.open(_WRITE_FILE_MODE_TO_OPEN_MODE[mode], encoding="utf-8") as fh:
            fh.write(content)
    except OSError as exc:
        return _emit_tool_error(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_id,
            running_event_uid=running_event_uid,
            code="write_failed",
            message=f"write_file: {exc}",
        )

    payload: dict[str, Any] = {
        "path": str(path),
        "mode": mode,
        "bytes_written": len(content.encode("utf-8")),
    }
    return _emit_tool_observation(
        conn=conn,
        lifecycle=lifecycle,
        action_id=action_id,
        running_event_uid=running_event_uid,
        payload=payload,
        semantics="ack",
    )


# --- Default registry assembly ----------------------------------------------

def build_default_registry(  # noqa: PLR0913 — every kwarg is a distinct D7 config value threaded into one tool's closure at registry-build time; bundling them into one options object defeats the point of each tool owning its own defaulted knobs.
    *,
    obsidian_vault_root: Path | None = None,
    web_search_max_results: int = DEFAULT_WEB_SEARCH_MAX_RESULTS,
    web_search_provider: str = DEFAULT_WEB_SEARCH_PROVIDER,
    web_search_api_key: str | None = None,
    web_fetch_max_bytes: int = DEFAULT_WEB_FETCH_MAX_BYTES,
    web_fetch_max_text_bytes: int = DEFAULT_WEB_FETCH_MAX_TEXT_BYTES,
    web_timeout_s: float = DEFAULT_WEB_TIMEOUT_S,
    vision_client: VisionClient | None = None,
    screen_max_width_px: int = DEFAULT_SCREEN_MAX_WIDTH_PX,
    confirmation_dispatch_outbox: bool = False,
    memory_db_path: Path | None = None,
    observed_repos: tuple[str, ...] = (),
    timesink_db_path: Path | None = None,
    work_state_refresh: WorkStateRefresh | None = None,
    daily_report_run: DailyReportRun | None = None,
) -> ToolRegistry:
    """Assemble the default ToolRegistry.

    The composition root (`jarvis.runtime`, Step 10) calls this once at
    startup and passes the registry to L3 + L4.

    Args:
        obsidian_vault_root: Vault root for `search_notes` (ADR-0011
            D7, `tools.obsidian.vault_root`). `None` (the default, and
            what the two existing integration tests pass implicitly)
            falls back to :data:`DEFAULT_OBSIDIAN_VAULT_ROOT` — the
            composition root always supplies the real configured
            value; a hand-built test registry can point this at a
            `tmp_path` fixture instead.
        web_search_max_results: `tools.web.search_max_results` (ADR-0011
            D7) — the `web_search` default absent a request argument.
        web_search_provider: `tools.web.search_provider` — which backend
            answers `web_search`. See
            :data:`DEFAULT_WEB_SEARCH_PROVIDER` for why the keyless
            default is not the recommended one.
        web_search_api_key: The credential named by
            `tools.web.search_api_key_env`, already resolved from the
            environment by the composition root. `None` with a keyed
            provider degrades to `ddgs` with a warning rather than
            failing boot.
        web_fetch_max_bytes: `tools.web.fetch_max_bytes` (ADR-0011 D7) —
            how much of the response body `web_fetch` drains off the
            socket. A memory bound, NOT the output cap.
        web_fetch_max_text_bytes: `tools.web.fetch_max_text_bytes` —
            `web_fetch`'s real output cap, applied to the text left
            after HTML extraction.
        web_timeout_s: `tools.web.timeout_s` (ADR-0011 D7) — shared by
            `web_search` and `web_fetch` (see :data:`DEFAULT_WEB_TIMEOUT_S`
            for why D5's per-tool 15s/20s split collapses to one knob).
        vision_client: Injected `screen_look` vision seam (ADR-0011
            D5/D7). `None` (the default, and what the two existing
            integration tests pass implicitly) means no usable
            `llm.presets.vision` block was found; `screen_look` still
            registers but every call degrades to an error observation
            AFTER saving the screenshot. `jarvis.runtime` always
            supplies a real client when `llm.presets.vision` parses.
        screen_max_width_px: `tools.screen.max_width_px` (ADR-0011 D7)
            — the `sips` downscale ceiling before the vision call.
        confirmation_dispatch_outbox: `realtime.concurrency_safety.
            confirmation_dispatch_outbox` — a lease-bearing dispatch
            admits through the outbox instead of a plain append.
        observed_repos: Currently configured Git repositories for activity coverage metadata.
        timesink_db_path: Optional existing TimeSink SQLite store, opened read-only.
        work_state_refresh: ADR 0023 — the runtime's one work-state refresh
            workflow; `None` (hand-built registries) leaves `refresh_work_state`
            off the menu.
        daily_report_run: ADR 0024 — the runtime's daily work report
            workflow; `None` leaves `daily_work_report` off the menu.
        memory_db_path: `memory.db_path` — registers `search_records`
            over that memory.db. `None` (hand-built test registries)
            registers no memory tool.
    """
    vault_root = (
        obsidian_vault_root if obsidian_vault_root is not None else DEFAULT_OBSIDIAN_VAULT_ROOT
    )
    registry = ToolRegistry(confirmation_dispatch_outbox=confirmation_dispatch_outbox)
    registry.register(get_current_time)
    registry.register(open_path)
    registry.register(
        ToolDefinition(
            name="search_notes",
            description=(
                "Case-insensitive full-text search over Allen's Obsidian vault "
                "(*.md files only). Returns matching lines with their source "
                "file. No index — brute-force scan, fine for a small vault."
            ),
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L0",
            result_semantics="observation",
            input_schema=_SEARCH_NOTES_INPUT_SCHEMA,
            handler=_make_search_notes_handler(vault_root),
            read_only=True,
            requires_entity=False,
            requires_confirmation=False,
        )
    )
    registry.register(
        ToolDefinition(
            name="read_file",
            description=(
                "Read a text file's contents by spoken name or description. "
                "The target is resolved to a canonical path before reading — "
                "never pass a raw filesystem path. Binary files are rejected; "
                "output is capped at 8 KiB."
            ),
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L0",
            result_semantics="observation",
            input_schema=_READ_FILE_INPUT_SCHEMA,
            handler=read_file_handler,
            read_only=True,
            requires_entity=True,
            requires_confirmation=False,
        )
    )
    registry.register(read_clipboard)
    registry.register(create_memo)
    registry.register(list_memos)
    registry.register(
        _make_web_search(
            default_max_results=web_search_max_results,
            timeout_s=web_timeout_s,
            provider=web_search_provider,
            api_key=web_search_api_key,
        )
    )
    registry.register(
        _make_web_fetch(
            max_bytes=web_fetch_max_bytes,
            max_text_chars=web_fetch_max_text_bytes,
            timeout_s=web_timeout_s,
            extract_api_key=(
                web_search_api_key if web_search_provider.strip().lower() == "tavily" else None
            ),
        )
    )
    registry.register(
        ToolDefinition(
            name="open_url",
            description="Open a URL in the default browser. Use for '用浏览器打开 X' requests.",
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L1",
            result_semantics="ack",
            input_schema=_OPEN_URL_INPUT_SCHEMA,
            handler=open_url_handler,
            read_only=False,
            requires_entity=False,
            requires_confirmation=False,
        )
    )
    registry.register(
        _make_screen_look(vision_client=vision_client, max_width_px=screen_max_width_px)
    )
    registry.register(
        ToolDefinition(
            name="write_file",
            description=(
                "Write text content to a file by spoken name, description, "
                "or path. The target is resolved to a canonical path before "
                "writing — never pass a raw filesystem path. mode='create' "
                "refuses an existing file; 'overwrite'/'append' both require "
                "one. Risk L3 — every dispatch requires Allen's explicit "
                "confirmation."
            ),
            # frozen 2026-09-12: engineering is off the LLM menu; no caller may reach this.
            allowed_callers=frozenset(),
            risk_level="L3",
            result_semantics="ack",
            input_schema=_WRITE_FILE_INPUT_SCHEMA,
            handler=write_file_handler,
            read_only=False,
            requires_entity=True,
            requires_confirmation=True,
        )
    )
    # Local import avoids a cycle: daily adapters use this module's flat Tool type.
    from jarvis.execution.daily_tools import (  # noqa: PLC0415
        build_daily_report_tool,
        build_daily_tools,
        build_work_state_tool,
    )

    for daily_tool in build_daily_tools(
        memory_db_path, repos=observed_repos, timesink_path=timesink_db_path
    ):
        registry.register(daily_tool)
    for state_tool in build_work_state_tool(work_state_refresh):
        registry.register(state_tool)
    for report_tool in build_daily_report_tool(daily_report_run):
        registry.register(report_tool)
    return registry


class ReadOnlyToolRegistry:
    """Read-only view of a :class:`ToolRegistry` for one turn (ADR-0016 D6).

    Exposes only tools with ``read_only=True``;
    ``dispatch`` of anything else raises :class:`UnknownToolError` before the
    inner registry emits ``action.dispatched``. The shared registry is not
    mutated: the composition root hands this view to ``decide()`` for turns
    whose ``surface.user_intent.channel`` is ``gpt_live``.
    """

    def __init__(self, inner: ToolRegistry) -> None:
        """Wrap ``inner``; nothing is copied, filtering happens per call."""
        self._inner = inner

    @staticmethod
    def _visible(tool_def: ToolDefinition | Tool) -> bool:
        return tool_def.read_only

    def get_definitions(self) -> tuple[ToolDefinition | Tool, ...]:
        """Every read-only tool, in registration order."""
        return tuple(t for t in self._inner.get_definitions() if self._visible(t))

    def for_caller(self, caller_principal: CallerPrincipal) -> tuple[ToolDefinition | Tool, ...]:
        """The caller's tools narrowed to the read-only ones."""
        return tuple(t for t in self._inner.for_caller(caller_principal) if self._visible(t))

    def dispatch(
        self,
        action_request: ActionRequest,
        conn: sqlite3.Connection,
        runtime_paths: RuntimePathsLike,
        lifecycle: ActionLifecycle,
    ) -> RawResultBundle:
        """Forward a read-only dispatch; refuse everything else without a trace."""
        if action_request.tool_name not in {t.name for t in self.get_definitions()}:
            LOGGER.warning(
                "read-only registry refused %r for action %s (ADR-0016 D6)",
                action_request.tool_name, action_request.action_id,
            )
            msg = f"unknown tool: {action_request.tool_name!r} (read-only view)"
            raise UnknownToolError(msg)
        return self._inner.dispatch(action_request, conn, runtime_paths, lifecycle)


__all__ = [
    "DEFAULT_OBSIDIAN_VAULT_ROOT",
    "DEFAULT_SCREEN_MAX_WIDTH_PX",
    "DEFAULT_WEB_FETCH_MAX_BYTES",
    "DEFAULT_WEB_FETCH_MAX_TEXT_BYTES",
    "DEFAULT_WEB_SEARCH_MAX_RESULTS",
    "DEFAULT_WEB_SEARCH_PROVIDER",
    "DEFAULT_WEB_TIMEOUT_S",
    "ActionLifecycle",
    "CallerNotAllowedError",
    "DuplicateToolError",
    "IllegalLifecycleTransition",
    "LifecycleState",
    "RawResult",
    "RawResultBundle",
    "ReadOnlyToolRegistry",
    "ResultSemantics",
    "RuntimePathsLike",
    "Tool",
    "ToolContext",
    "ToolDefinition",
    "ToolError",
    "ToolRegistry",
    "ToolRegistryError",
    "UnknownToolError",
    "VisionClient",
    "build_default_registry",
    "create_memo",
    "get_current_time",
    "list_memos",
    "live_action_ids",
    "open_path",
    "open_url_handler",
    "read_clipboard",
    "read_file_handler",
    "register_live_action",
    "release_turn_actions",
    "tool",
    "tool_error",
    "tool_result",
    "turn_action_ids",
    "validate_egress_url",
    "write_file_handler",
]
