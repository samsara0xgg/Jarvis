"""L4 Capability Execution — ToolRegistry + ActionLifecycle + handlers.

This module is the SINGLE place in `jarvis/` allowed to dispatch L4 tool
handlers.

Per spec.html §3.4 (`ActionRequest` / lifecycle / ToolRegistry) +
spec.html §3.5 (CallerPrincipal scoping) + ADR 0001 § Acceptance B
(lifecycle) + ADR 0019 (the flat `Tool` shape).

One handler shape: `Tool` (ADR 0019) — a function of its arguments; the
dispatcher emits every event, moves the lifecycle and serializes the payload.

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

import base64
import binascii
import ipaddress
import json
import logging
import math
import re
import socket
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol
from urllib.parse import urlsplit, urlunsplit

from jarvis.execution.path_resolver import TargetKind, load_file_targets_config
from jarvis.execution.path_resolver import resolve as resolve_path_target
from jarvis.shared import (
    ActionRequest,
    CallerPrincipal,
    RawResult,
    ResultSemantics,
    RiskLevel,
    lang,
)
from jarvis.shared.action_admission import action_admission_guard
from jarvis.shared.clock import Clock, SystemClock
from jarvis.shared.device_link import DeviceCallError
from jarvis.shared.receipts import (
    Completed,
    FailureKind,
    Receipt,
    call_asks,
    derive,
    failure_kind,
    only_reads,
)
from jarvis.shared.text import truncate_utf8
from jarvis.state.authorized_dispatch_outbox import (
    ConfirmationRevalidationError,
    admit_authorized_dispatch,
    answer_confirmation_once,
)
from jarvis.state.event_log import emit_event, iter_events_of_types
from jarvis.state.lifecycle_terminal import terminalize_action
from jarvis.state.places import read_kept
from jarvis.state.projections import (
    CLARIFICATION_EVENT_TYPES,
    PendingClarification,
    PendingConfirmations,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Sequence
    from datetime import time as clock_time

    from jarvis.execution.around import Surroundings
    from jarvis.execution.bus_tool import RideControl
    from jarvis.execution.highlight_tool import HighlightMarker
    from jarvis.execution.mail_draft_tool import DraftStore
    from jarvis.execution.night_tools import NightControl
    from jarvis.execution.transit_tool import BusTimes, TransitOffers
    from jarvis.execution.weather_tool import WeatherRequest
    from jarvis.execution.web_search_tool import DeepSearch
    from jarvis.shared import Event
    from jarvis.shared.device_link import DeviceLink
    from jarvis.shared.here import Here, HereSource
    from jarvis.state.voice_settings import VoiceSettings


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
          screenshots under it).
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


# --- Tool (ADR 0019: the flat definition) -------------------------------------


@dataclass(frozen=True)
class ToolContext:
    """What a flat handler may reach beyond its arguments."""

    conn: sqlite3.Connection
    runtime_paths: RuntimePathsLike
    action_id: str
    clock: Clock
    """The one clock (ADR 0322): a tool takes the time of day from here and nowhere else."""
    target_entity_ref: str | None = None
    """The ref the Pre-action Gate trusted for a ``requires_entity`` tool; ``None`` otherwise."""
    here: Here | None = None
    """Where he is for this call (ADR 0322); ``None`` on a registry built without a source."""


class Receipted(dict[str, Any]):
    """A handler's payload with its own receipt (ADR 0319): what it did, in its own words."""

    def __init__(self, payload: Mapping[str, Any], receipt: Receipt) -> None:
        """Hold the payload as the dict it is; the receipt rides beside it."""
        super().__init__(payload)
        self.receipt = receipt


type FlatHandler = Callable[[Mapping[str, Any], ToolContext], Mapping[str, Any]]
"""A tool is a function of its arguments; everything else is the dispatcher's. It may return
:class:`Receipted` to give its own receipt; otherwise the dispatcher derives one."""

type WorkStateRefresh = Callable[[Mapping[str, Any], ToolContext], dict[str, Any]]
"""ADR 0023: the runtime's one refresh workflow, injected into ``refresh_work_state``."""

type DailyReportRun = Callable[[Mapping[str, Any], ToolContext], dict[str, Any]]
"""ADR 0024: the runtime's daily-report workflow, injected into ``daily_work_report``."""

DEFAULT_MAX_RESULT_CHARS: Final[int] = 8192
"""Serialized-result budget of a flat tool that declares none (the 8 KiB every D5 tool used)."""

_MIN_WINDOW_CHARS: Final[int] = 80
"""Below this a head+tail window is all marker; the dispatcher shrinks the next string instead."""


class ToolError(Exception):
    """A tool-level failure the model reads as ``{"error": ..., "code": ..., "kind": ...}``.

    ``kind`` is one of the spec's four failures (ADR 0319); a site that names none gets the
    kind its ``code`` stands for from :func:`jarvis.shared.receipts.failure_kind`.
    """

    def __init__(
        self, message: str, *, code: str = "tool_error", kind: FailureKind | None = None,
    ) -> None:
        """Keep ``code`` as the short tag ``action.result_observed.error`` carries."""
        super().__init__(message)
        self.code = code
        self.kind: FailureKind = kind or failure_kind(code)


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
    reads: Callable[[Mapping[str, Any]], bool] | None = None
    """Whether a call with these arguments only reads (ADR 0319), for a tool that reads or
    writes by argument; ``None`` keeps ``read_only`` for every call."""
    supersedes: Mapping[str, str] = field(default_factory=dict)
    """Deferred tools whose job this one now does, each with how to call this one instead
    (``{"mcp__gmail__gmail_search": "mail(query=...)"}``). While this tool is on the menu,
    ``tool_search`` loads none of them and says to call this one (ADR 0242)."""
    effect: Callable[[Mapping[str, Any]], RiskLevel] | None = None
    """The level of a call with these arguments (ADR 0361), for a tool whose calls differ: L0, L1
    or L3. ``risk_level`` is then the highest of them. ``None``: one level for every call."""
    complete: Callable[[Mapping[str, Any], ToolContext], Mapping[str, Any]] | None = None
    """Before a call that needs a card is frozen (ADR 0361): the full arguments it will run
    with, or a :class:`ToolError` saying why it cannot. It only reads."""


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


def _list_slots(node: Any, path: str, found: list[tuple[str, list[Any]]]) -> None:  # noqa: ANN401 — walks any JSON-shaped value.
    """Collect every non-empty list in a JSON-shaped payload with its key path (``a[2].rows``)."""
    items: Any = node.items() if isinstance(node, dict) else enumerate(node)
    for key, value in items:
        here = f"{path}[{key}]" if isinstance(node, list) else f"{path}.{key}" if path else str(key)
        if isinstance(value, list) and value:
            found.append((here, value))
        if isinstance(value, (dict, list)):
            _list_slots(value, here, found)


def _size(value: Any) -> int:  # noqa: ANN401 — any JSON-shaped value.
    return len(json.dumps(value, ensure_ascii=False))


def _fit_result(payload: dict[str, Any], cap: int) -> dict[str, Any]:
    """Shrink the payload until ``tool_result(payload)`` fits ``cap``.

    A list loses whole items from its end and the payload says so in ``cut``
    (``{"<key path>": {"shown": n, "more": m}}``): an item's text is never cut. One long
    string, such as a page, is still windowed head+tail and the payload marked ``truncated``.
    The biggest of the two goes first.
    """
    while (over := len(tool_result(payload)) - cap) > 0:
        lists: list[tuple[str, list[Any]]] = []
        _list_slots(payload, "", lists)
        slots: list[tuple[Any, Any]] = []
        _string_slots(payload, slots)
        candidates = [(c, k) for c, k in slots if len(c[k]) > _MIN_WINDOW_CHARS]
        path, items = max(lists, key=lambda found: _size(found[1]), default=("", []))
        if items and (
            not candidates or _size(items) >= max(len(c[k]) for c, k in candidates)
        ):
            gone = freed = 0
            while items and freed < over:
                freed += _size(items.pop()) + 2
                gone += 1
            cut = payload.setdefault("cut", {})
            cut[path] = {"shown": len(items), "more": cut.get(path, {}).get("more", 0) + gone}
            continue
        if not candidates:
            break
        container, key = max(candidates, key=lambda ck: len(ck[0][ck[1]]))
        payload["truncated"] = True
        keep = max(_MIN_WINDOW_CHARS, len(container[key]) - over)
        container[key] = _head_tail(container[key], keep)
    return payload


def _unexpected(tool_name: str, exc: Exception) -> str:
    """What the model is told when a tool's code failed in a way the tool did not name."""
    return (
        f"{tool_name} failed unexpectedly ({type(exc).__name__}: {exc}); "
        "tell him it failed, and do not repeat the same call"
    )


def _call_reads(tool_def: Tool, arguments: Mapping[str, Any]) -> bool:
    """Whether this call only reads; a ``reads`` that cannot take these arguments means a write."""
    try:
        return only_reads(tool_def, arguments)
    except Exception:  # noqa: BLE001 — arguments the model made up must not strand the lifecycle.
        return False


def _observe(  # noqa: PLR0913 — the four handler arguments plus the definition and the uid the dispatcher holds.
    tool_def: Tool,
    action_request: ActionRequest,
    conn: sqlite3.Connection,
    runtime_paths: RuntimePathsLike,
    lifecycle: ActionLifecycle,
    *,
    running_event_uid: str,
    clock: Clock,
    here: HereSource | None,
) -> RawResult:
    """Run a flat tool and write its one terminal, ``action.result_observed``.

    The terminal row is the dispatcher's whatever the handler did, so the
    lifecycle never strands at ``running`` (ADR-0011 §5 "never a crash"): a
    non-``ToolError`` exception becomes an ``unexpected_error`` observation
    and is logged with its traceback. The row carries the call's receipt (ADR 0319): the
    tool's own when its handler returned :class:`Receipted`, else one derived from the call.
    """
    action_id = action_request.action_id
    arguments = action_request.arguments
    cap = tool_def.max_result_chars
    reads = _call_reads(tool_def, arguments)
    error: str | None = None
    kind: FailureKind = "dont_retry"
    message = ""
    payload: dict[str, Any] = {}
    tool_output = ""
    receipt: Receipt | None = None
    ctx = ToolContext(
        conn, runtime_paths, action_id, clock, action_request.target_entity_ref,
        None if here is None else here.bind(action_request.turn_id),
    )
    try:
        returned = tool_def.handler(arguments, ctx)
        if isinstance(returned, Receipted):
            receipt = returned.receipt
        payload = _fit_result(dict(returned), cap)
        tool_output = tool_result(payload)
    except ToolError as exc:
        error, message, kind = exc.code, str(exc), exc.kind
    except Exception as exc:
        # The terminal row is the dispatcher's whatever the handler did.
        LOGGER.exception("tool %s raised", tool_def.name)
        error = "unexpected_error"
        message = _unexpected(tool_def.name, exc)
    if error is not None:
        tool_output = tool_result(
            _fit_result({"error": message, "code": error, "kind": kind}, cap),
        )
        payload = {"error": error}
        receipt = derive(tool_def.name, arguments, reads=reads, code=error, failure=kind)
    elif receipt is None:
        receipt = derive(tool_def.name, arguments, reads=reads)
    if ctx.here is not None and ctx.here.used is not None:
        receipt = replace(receipt, here=ctx.here.used)  # the dispatcher's, never the tool's
    semantics: ResultSemantics = "observation" if error is None else "error"
    event_payload: dict[str, Any] = {
        "action_id": action_id,
        "semantics": semantics,
        "tool_output": tool_output,
        "receipt": receipt.to_json(),
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
        receipt=receipt,
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


@tool(
    description="Read the current local date and time (observation only).",
    input_schema={"type": "object", "properties": {}, "required": []},
    # Not the model's: its state line gives the time every turn, and on
    # 2026-09-30 reading the clock cost 3 of 8 voice turns a request round.
    allowed_callers=frozenset({CallerPrincipal.REGEX_ROUTER}),
    risk_level="L0",
    read_only=True,
)
def get_current_time(_args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Read the system clock (spec §3.5.4) for Tier 0.

    The payload carries the machine keys ``iso`` / ``date`` / ``time`` /
    ``weekday`` (English) plus the TTS-ready ``spoken_time`` / ``spoken_date``,
    in the language setting, that the Tier 0 replies read.
    """
    now = ctx.clock.now()
    return {
        "iso": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "weekday": lang.weekday(now, "en"),
        "spoken_time": lang.spoken_time(now),
        "spoken_date": lang.spoken_date(now),
    }


# --- ask card (ask_user) — ADR 0066 ------------------

_ASK_MAX_FIELDS: Final[int] = 4
_ASK_TEXT_MAX_CHARS: Final[int] = 200
_ASK_LABEL_MAX_CHARS: Final[int] = 40
_ASK_CHOICES: Final[range] = range(2, 7)
# ponytail: a word list over the question and labels; a field phrased around it
# gets through (ADR 0066 Consequences). Sign-ins and payment stay with ADR 0059.
_ASK_SECRET_RE: Final[re.Pattern[str]] = re.compile(
    r"password|passcode|passphrase|\bpin\b|cvv|cvc|card number|credit card|debit card"
    r"|security code|verification code|one[- ]time code|\botp\b|api key|secret|token"
    r"|密码|口令|卡号|信用卡|银行卡|安全码|验证码|动态码|密钥|令牌",
    re.IGNORECASE,
)


def _ask_text(value: object, name: str, limit: int, fix: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > limit:
        msg = f"ask_user: {name} must be 1-{limit} characters; {fix}"
        raise ToolError(msg, code="invalid_arguments")
    return text


def _ask_field(raw: object) -> dict[str, Any]:
    """One card field as the companion draws it: label, choices, pre-filled value, remember."""
    if not isinstance(raw, Mapping):
        msg = 'ask_user: each entry in fields must be an object, e.g. {"label": "delivery address"}'
        raise ToolError(msg, code="invalid_arguments")
    field: dict[str, Any] = {
        "label": _ask_text(
            raw.get("label"), "label", _ASK_LABEL_MAX_CHARS,
            "name the detail in a few words, e.g. delivery address",
        ),
        "remember": raw.get("remember") is not False,
    }
    choices = raw.get("choices")
    if choices:
        if not isinstance(choices, list) or len(choices) not in _ASK_CHOICES:
            msg = "ask_user: choices must list 2-6 options; leave choices out for a typed answer"
            raise ToolError(msg, code="invalid_arguments")
        field["choices"] = [
            _ask_text(c, "a choice", _ASK_LABEL_MAX_CHARS, "shorten the option")
            for c in choices
        ]
    value = raw.get("value")
    if isinstance(value, str) and value.strip():
        field["value"] = value.strip()[:_ASK_TEXT_MAX_CHARS]
    return field


def _turn_of(action_id: str) -> str | None:
    """The live turn driving ``action_id``, or None outside a turn."""
    with _LIVE_ACTIONS_LOCK:
        return next((t for t, ids in _LIVE_ACTIONS_BY_TURN.items() if action_id in ids), None)


@tool(
    description=(
        "Ask the user for details you need before you can go on (an address, amounts, dates, "
        "how many people, which of a few options) on a card with one input per detail. Use it "
        "whenever your reply would otherwise end by asking for such details, even where you "
        "asked in words before: the user fills in the card instead of typing. The card stays on "
        "the user's screen until they answer or close it, or you take it down. The turn then "
        "ends: say in one short sentence what you need and stop. The answers arrive as the "
        "user's next message. Never ask for passwords, card numbers, codes or keys, and never "
        "use it to confirm an action: actions that need approval get their own card."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "One short sentence at the top of the card, in the language "
                    "you answer in."
                ),
            },
            "fields": {
                "type": "array",
                "minItems": 1,
                "maxItems": _ASK_MAX_FIELDS,
                "description": "One entry per detail.",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {
                            "type": "string",
                            "description": (
                                "Short name of the detail in the language you answer "
                                "in, e.g. delivery address. A remembered answer is "
                                "kept under it."
                            ),
                        },
                        "choices": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "2-6 options when the answer is one of a few, in the "
                                "same language as the question."
                            ),
                        },
                        "value": {
                            "type": "string",
                            "description": (
                                "An answer you already know, pre-filled for the user to "
                                "change. Never an example or a hint: it is sent as the answer."
                            ),
                        },
                        "remember": {
                            "type": "boolean",
                            "description": (
                                "Keep the answer in [About the user]. true only for facts "
                                "about the user that stay true (address, postal code, "
                                "allergies); false for anything about this one request "
                                "(dates, times, amounts, how many, this order's choices). "
                                "Default true."
                            ),
                        },
                    },
                    "required": ["label"],
                },
            },
        },
        "required": ["question", "fields"],
    },
    allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
    risk_level="L1",
    read_only=False,
)
def ask_user(args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Put up the ask card (`clarification.requested`); a newer one replaces it."""
    question = _ask_text(
        args.get("question"), "question", _ASK_TEXT_MAX_CHARS, "make it one short sentence",
    )
    raw_fields = args.get("fields")
    if not isinstance(raw_fields, list) or not 1 <= len(raw_fields) <= _ASK_MAX_FIELDS:
        msg = (
            f"ask_user: fields must list 1-{_ASK_MAX_FIELDS} details; ask for the rest in words "
            "after he has answered"
        )
        raise ToolError(msg, code="invalid_arguments")
    fields = [_ask_field(raw) for raw in raw_fields]
    if _ASK_SECRET_RE.search(" ".join([question, *(f["label"] for f in fields)])):
        msg = (
            "ask_user: a card never asks for passwords, card numbers, codes or keys; "
            "tell him to enter those on the site itself"
        )
        raise ToolError(msg, code="secret_field")
    emit_event(
        ctx.conn,
        type="clarification.requested",
        payload={
            "clarification_id": f"Q{uuid.uuid4().hex[:12]}",
            "question": question,
            "fields": fields,
            "turn_id": _turn_of(ctx.action_id) or "",
            "action_id": ctx.action_id,
        },
        source_event_id=_get_running_event_uid(ctx.conn, ctx.action_id),
        correlation={"action_id": ctx.action_id},
    )
    return {
        "status": "card_shown",
        "next": (
            "The card is on screen. Tell the user in one short sentence what you need, then "
            "stop; their answers arrive as their next message."
        ),
    }


# ADR 0075: what the waiting confirmation card folds from (ADR 0062).
_CARD_TYPES: Final[tuple[str, ...]] = (
    "confirmation.requested", "confirmation.accepted", "confirmation.rejected", "gate.evaluated",
)


@tool(
    description=(
        "Take down the card waiting on Allen's screen without running anything: your question "
        "card (ask_user) once his words answered it or it no longer applies, or a tool's "
        "confirmation card when he says not to go ahead or that it is wrong. To change a "
        "confirmation card call its tool again instead: that replaces it. Leave a card up when "
        "his words are about something else. No arguments; with no card waiting it does "
        "nothing and says so."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
    allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
    risk_level="L1",
    read_only=False,
)
def dismiss_card(_args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Take down the waiting confirmation card (ADR 0075) and question card (ADR 0206), if any."""
    down: list[str] = []
    slot = PendingConfirmations.from_events(iter_events_of_types(ctx.conn, _CARD_TYPES)).slot
    turn_id = _turn_of(ctx.action_id)
    late = False
    if slot is not None and slot.is_live(int(ctx.clock.time() * 1000)):
        try:
            answer_confirmation_once(
                ctx.conn,
                confirmation_id=slot.confirmation_id,
                accepted=False,
                utterance_raw="",
                grammar_rule_id="card_withdrawn",
                correlation={"turn_id": turn_id} if turn_id else None,
            )
            names = ", ".join(str(call.get("tool_name", "")) for call in slot.calls)
            down.append(f"the {names} confirmation card (not run)")
        except ConfirmationRevalidationError:
            late = True
    question = PendingClarification.from_events(
        iter_events_of_types(ctx.conn, CLARIFICATION_EVENT_TYPES),
    )
    # A bus card (ADR 0205) asks him nothing.
    if question is not None and question.waiting and question.trip is None:
        emit_event(
            ctx.conn,
            type="clarification.withdrawn",
            payload={
                "clarification_id": question.clarification_id,
                "turn_id": turn_id or "",
                "action_id": ctx.action_id,
            },
            source_event_id=_get_running_event_uid(ctx.conn, ctx.action_id),
            correlation={"action_id": ctx.action_id},
        )
        down.append(f'the question card "{question.question}"')
    if down:
        return {"summary": f"Took down {' and '.join(down)}.", "dismissed": down}
    if late:
        summary = (
            "The card was answered or replaced a moment ago, so nothing was taken down; "
            "do not tell him it was cancelled."
        )
        return {"summary": summary, "dismissed": []}
    return {"summary": "No card is waiting; nothing to do.", "dismissed": []}


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
    catches both and turns them into a `tool_error(code="invalid_arguments")`
    RawResult (see `list_tasks_handler`'s `invalid_arguments` shape for the
    convention this mirrors). The schema's `enum`/`required` should catch
    most of this first for `jarvis_llm` callers, but Tier 0's literal-string
    `args` (spec §17) — and a Tier 0 capture group that strips to an empty
    string, e.g. "帮我打开 。" — bypass schema validation entirely, so the
    handler must defend here rather than let an unhandled exception strand
    the lifecycle at `running`.
    """
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        msg = f"query must be the spoken name of the file or folder to open (got {query!r})"
        raise TypeError(msg)

    target_kind = arguments.get("target_kind", _OPEN_PATH_DEFAULT_TARGET_KIND)
    if target_kind not in ("file", "folder", "any"):
        msg = f"target_kind must be one of: file, folder, any (got {target_kind!r})"
        raise TypeError(msg)

    app = arguments.get("app", _OPEN_PATH_DEFAULT_APP)
    if app not in ("default", "vscode"):
        msg = f"app must be one of: default, vscode (got {app!r})"
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
        "Open a file or folder on the user's Mac by spoken name (bookmark "
        "alias, partial filename, or description). Use when the user asks to "
        "open X, or to open X in VS Code."
    ),
    input_schema=_OPEN_PATH_INPUT_SCHEMA,
    allowed_callers=frozenset({CallerPrincipal.REGEX_ROUTER, CallerPrincipal.JARVIS_LLM}),
    risk_level="L1",
    read_only=False,
    # requires_entity stays False deliberately: open_path keeps its own
    # resolve-then-act contract internally — it *is* a resolver caller.
)
def open_path(args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Open a file or folder on the user's Mac by spoken name (spec §17 companion tool).

    "打开 X" / "用 VS Code 打开 X" — the query is resolved via
    :func:`jarvis.execution.path_resolver.resolve` (pure, no events; see
    that module for the bookmark / Spotlight / one-level-scan strategy),
    then handed to macOS `open`. Errors: ``invalid_arguments`` (bad
    query / target_kind / app), ``target_not_found``, ``open_failed``.
    """
    try:
        parsed = _parse_open_path_args(args)
    except TypeError as exc:
        msg = f"open_path: {exc}"
        raise ToolError(msg, code="invalid_arguments") from exc

    target = resolve_path_target(parsed.query, parsed.target_kind, ctx.conn)
    if target is None:
        msg = (
            f"open_path: no file or folder matched {parsed.query!r}; ask him what it is called, "
            "or try a shorter part of the name"
        )
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
        msg = f"open_path: macOS could not run open ({exc}); tell him it did not open"
        raise ToolError(msg, code="open_failed") from exc
    if proc.returncode != 0:
        stderr_tail = (proc.stderr or "").strip()[-_OUTPUT_TAIL_BYTES:]
        msg = (
            f"open_path: macOS open failed (exit {proc.returncode}: {stderr_tail}); "
            "tell him it did not open"
        )
        raise ToolError(msg, code="open_failed")

    return {
        "opened_name": target.display_name,
        "opened_path": str(target.path),
        "app_used": app_used,
        "target_kind": parsed.target_kind,
    }


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


def _search_notes_tool(handler: FlatHandler) -> Tool:
    return Tool(
        name="search_notes",
        description=(
            "Case-insensitive full-text search over the user's Obsidian vault "
            "(*.md files only). Returns matching lines with their source "
            "file. No index — brute-force scan, fine for a small vault."
        ),
        input_schema=_SEARCH_NOTES_INPUT_SCHEMA,
        handler=handler,
        allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
        risk_level="L0",
        read_only=True,
        max_result_chars=2 * _SEARCH_NOTES_TOTAL_MAX_BYTES,
    )


def _make_search_notes(vault_root: Path) -> Tool:  # noqa: C901 — one linear validate/search/rank pass; splitting scatters the fail-fast checks from the loop they guard.
    """Bind ``vault_root`` into the ``search_notes`` tool (ADR-0011 D7).

    ``search_notes`` is the only ADR-0011 D5 tool that needs a config
    value the request surface doesn't already carry. L4 handlers do not
    load YAML themselves: the composition root
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

    def search_notes(args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:  # noqa: C901, PLR0912 — one linear validate/search/rank pass (containment + encoding + per-line + total-cap guards all live in the loop they protect); splitting them out scatters the fail-fast checks from the data they guard, same rationale as the outer function's own noqa.
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            msg = (
                "search_notes: query must be the words to look for in his notes "
                f"(got {query!r})"
            )
            raise ToolError(msg, code="invalid_arguments")

        max_results = _SEARCH_NOTES_DEFAULT_MAX_RESULTS
        max_results_arg = args.get("max_results")
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
            return {
                "results": [],
                "note": (
                    f"The notes folder {vault_root} does not exist (renamed or moved?); "
                    "tell him his notes cannot be searched until that is fixed."
                ),
            }

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

        payload: dict[str, Any] = {
            "results": _cap_rows_total_bytes(results, _SEARCH_NOTES_TOTAL_MAX_BYTES),
        }
        if non_utf8_files:
            payload["non_utf8_files"] = non_utf8_files
        return payload

    return _search_notes_tool(search_notes)


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


@tool(
    description=(
        "Read a text file's contents by spoken name or description. "
        "The target is resolved to a canonical path before reading — "
        "never pass a raw filesystem path. Binary files are rejected; "
        "output is capped at 8 KiB."
    ),
    input_schema=_READ_FILE_INPUT_SCHEMA,
    allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
    risk_level="L0",
    read_only=True,
    requires_entity=True,
    max_result_chars=3 * _READ_FILE_MAX_BYTES,
)
def read_file(_args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """L0 `read_file` — the first `requires_entity=True` tool (ADR-0011 D5).

    Reads the RESOLVED canonical path off `ctx.target_entity_ref` — NEVER
    `arguments["target"]`, the raw free text the LLM supplied. That is the
    entire point of D4's resolve-on-propose contract and invariant I2 (no
    fabricated entity IDs): by the time this handler runs, the Pre-action
    Gate has already required a trusted ``"file:<abs-path>"`` ref, so the
    raw argument is never even read here.

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
    ref = ctx.target_entity_ref
    if ref is None or not ref.startswith(_READ_FILE_ENTITY_PREFIX):
        msg = "read_file: no file was resolved from that name; ask him which file he means"
        raise ToolError(msg, code="no_resolved_target")

    path = Path(ref[len(_READ_FILE_ENTITY_PREFIX) :])
    if not path.is_file():
        msg = (
            f"read_file: {path} does not exist or is not a regular file; "
            "ask him which file he means"
        )
        raise ToolError(msg, code="file_not_found")

    try:
        total_bytes = path.stat().st_size
        with path.open("rb") as fh:
            prefix = fh.read(_READ_FILE_MAX_BYTES)
    except OSError as exc:
        msg = f"read_file: {path} could not be read ({exc}); tell him it failed"
        raise ToolError(msg, code="read_failed") from exc

    if b"\x00" in prefix:
        msg = f"read_file: {path} is a binary file; tell him you can only read text files"
        raise ToolError(msg, code="binary_file")

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
    return payload


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
        msg = f"read_clipboard: the clipboard could not be read ({exc}); tell him it failed"
        raise ToolError(msg, code="clipboard_read_failed") from exc
    if proc.returncode != 0:
        stderr_tail = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        msg = (
            f"read_clipboard: the clipboard could not be read (exit {proc.returncode}: "
            f"{stderr_tail}); tell him it failed"
        )
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


def resolve_hostname_ips(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
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
    `normalize_egress_url` both call this SAME function, so the string
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


def normalize_egress_url(url: str) -> str | None:
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
    ] = resolve_hostname_ips,
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
    `normalize_egress_url(url)`, never `url` itself (see
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


# --- web_search / web_fetch defaults ---------------------------------------------

DEFAULT_WEB_SEARCH_MAX_RESULTS: Final[int] = 5
"""Default `max_results` absent both a request argument and a
`tools.web.search_max_results` config override (ADR-0011 D7). Public so
`jarvis.runtime` can fall back to it — same single-source-of-truth
reasoning as `DEFAULT_OBSIDIAN_VAULT_ROOT` (NIT-FIX 8, ADR-0011 §12)."""

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

DEFAULT_WEB_TIMEOUT_S: Final[float] = 20.0
"""Shared `web_search`/`web_fetch` timeout absent a `tools.web.timeout_s`
config override. ADR-0011 D5's prose gives the two tools DIFFERENT
timeouts (15s / 20s) but D7's shipped config schema has only ONE
`timeout_s` key under `tools.web` — deliberately unprefixed, unlike its
`search_max_results`/`fetch_max_bytes` siblings, which reads as intent
to share one knob. Step 6 resolves the conflict by applying the single
configured value to both tools (ADR-0011 §12 errata candidate)."""


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


@tool(
    description=(
        "Open a URL in the default browser. Use when the user asks to open X"
        " in the browser."
    ),
    input_schema=_OPEN_URL_INPUT_SCHEMA,
    allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
    risk_level="L1",
    read_only=False,
)
def open_url(args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
    """Open a URL in the default browser (ADR-0011 D5).

    Same scheme + address checks as `web_fetch` via `validate_egress_url`
    — `open http://127.0.0.1:8006/shutdown` would otherwise open Allen's
    browser against his own daemon. `open` is handed
    `normalize_egress_url`'s canonical re-serialization of `url`, never
    `url` itself (ADR-0011 §12 MUST-FIX 1/2 root cause) — the browser's
    WHATWG URL parser reads some strings (a backslash before the last
    `@` in the authority, in particular) differently from the
    `urlsplit`-based guard that just validated them; canonicalizing
    strips exactly the userinfo/fragment components that trick relies
    on. The browser actually opening is not verified, same posture
    `open_path` documents for its own `open` call.
    """
    url = args.get("url")
    if not isinstance(url, str) or not url.strip():
        msg = f"open_url: url must be the page address, http or https (got {url!r})"
        raise ToolError(msg, code="invalid_arguments")

    allowed, reason = validate_egress_url(url)
    if not allowed:
        msg = (
            f"open_url: {url!r} was refused ({reason}); only public http(s) pages "
            "can be opened"
        )
        raise ToolError(msg, code="ssrf_guard_refused")

    normalized = normalize_egress_url(url)
    if normalized is None:
        # Should be unreachable — the guard just allowed `url` via
        # the identical parser — but refuse rather than ever fall
        # back to the raw string.
        msg = f"open_url: {url!r} is not a usable address; check the URL"
        raise ToolError(msg, code="ssrf_guard_refused")

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
        msg = f"open_url: macOS could not run open ({exc}); tell him it did not open"
        raise ToolError(msg, code="open_failed") from exc

    if proc.returncode != 0:
        stderr_tail = (proc.stderr or "").strip()[-_OUTPUT_TAIL_BYTES:]
        msg = (
            f"open_url: macOS open failed (exit {proc.returncode}: {stderr_tail}); "
            "tell him it did not open"
        )
        raise ToolError(msg, code="open_failed")

    return {"url": normalized}


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

    def describe_image(
        self, image_path: Path, *, question: str | None, instructions: str | None = None,
    ) -> str:
        """Return a text description of the image at `image_path`.

        `instructions` replaces the screen-reading system prompt (ADR 0223: a picture he sent).
        """
        ...


DEFAULT_SCREEN_MAX_WIDTH_PX: Final[int] = 1568
"""Default `tools.screen.max_width_px` (ADR-0011 D7) — the vision
model's own recommended upper bound on input image width."""

SCREEN_ARTIFACTS_DIRNAME: Final[str] = "screen_artifacts"
"""Subdirectory of `runtime_paths.artifacts_root`. The runtime deletes
screenshots older than `tools.screen.retention_days` (ADR 0067)."""

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


def _capture_screen(image_path: Path, max_width_px: int) -> None:
    """Capture the main display into `image_path` and downscale it; raise `ToolError` if not.

    The device half of `screen_look`: on a terminal it is all the tool does (ADR 0170),
    on one machine it is followed by the vision call.
    """
    try:
        capture_proc = _run_screencapture(image_path, timeout_s=_SCREEN_CAPTURE_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        msg = f"screen_look: the screen could not be captured ({exc}); tell him it failed"
        raise ToolError(msg, code="screen_capture_start_failed") from exc
    if capture_proc.returncode != 0:
        # The cause of a non-zero exit is not determinable from here (TCC
        # denial, bad path, full disk...): report what happened and name
        # Screen Recording only as the likely first-run cause.
        stderr_tail = (capture_proc.stderr or b"").decode("utf-8", errors="replace").strip()
        msg = (
            f"screen_look: the screen could not be captured (exit {capture_proc.returncode}"
            f"{f': {stderr_tail}' if stderr_tail else ''}). On a first call the likely cause is "
            f"missing Screen Recording permission for {sys.executable} (System Settings → "
            "Privacy & Security); tell him which, or that it failed."
        )
        raise ToolError(msg, code="screen_capture_failed")
    if not image_path.is_file() or not _looks_like_png(image_path):
        # On some macOS versions a TCC-denied screencapture exits 0 but
        # writes nothing (or a truncated file): that shape IS specific
        # enough to name Screen Recording as the cause.
        msg = (
            f"screen_look: {_screen_recording_permission_message()} "
            "(the capture produced no image); tell him to allow it, then ask again"
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


_BLANK_BYTES_PER_PIXEL: Final[float] = 0.01
"""A PNG of one flat colour (a sleeping display captures black) takes about 0.005 bytes a pixel;
a screen with anything on it takes ten times that or more."""


def _screen_is_blank(image_path: Path) -> bool:
    """Whether the screenshot is one flat colour, judged from its size against its pixels."""
    header = image_path.read_bytes()[:24]
    if header[12:16] != b"IHDR":  # a PNG's first chunk holds its width and height
        return False
    pixels = int.from_bytes(header[16:20], "big") * int.from_bytes(header[20:24], "big")
    return pixels > 0 and image_path.stat().st_size < pixels * _BLANK_BYTES_PER_PIXEL


def _describe_screen(
    vision_client: VisionClient | None, image_path: Path, question: str | None,
) -> dict[str, Any]:
    """The vision half of `screen_look`: the saved screenshot in, `artifact_path` + text out.

    `vision_client=None` means `jarvis.runtime` found no usable
    `llm.presets.vision` block: the tool still registers (the menu stays
    complete) but every call degrades to a `vision_unconfigured` error —
    AFTER the screenshot is captured and saved, so evidence survives a
    config gap the same way it survives a live proxy outage.
    """
    if vision_client is None:
        msg = (
            "screen_look: the screen was captured but no vision model is configured "
            f"(llm.presets.vision); tell him you cannot describe it. Saved at {image_path}"
        )
        raise ToolError(msg, code="vision_unconfigured")
    try:
        description = vision_client.describe_image(image_path, question=question)
    except Exception as exc:  # degrades to an error; the screenshot is on disk regardless.
        msg = (
            "screen_look: the screen was captured but the vision model failed "
            f"({type(exc).__name__}: {exc}); tell him it failed. Saved at {image_path}"
        )
        raise ToolError(msg, code="vision_call_failed") from exc
    return {"artifact_path": str(image_path), "description": description}


SCREEN_CAPTURE_MAX_IMAGE_BYTES: Final[int] = 4 * 1024 * 1024
"""ADR 0170: the largest screenshot a terminal sends the brain (after the downscale)."""


def _screenshot_from_device(link: DeviceLink) -> bytes:
    """Ask the terminal for a screenshot and decode it; a bad reply is a `ToolError`."""
    try:
        reply = link("screen_capture", {}, None)
    except DeviceCallError as exc:
        raise ToolError(str(exc), code=exc.code) from exc
    encoded = reply.get("image_b64")
    if not isinstance(encoded, str) or len(encoded) > SCREEN_CAPTURE_MAX_IMAGE_BYTES * 4 // 3 + 4:
        msg = "screen_look: his computer sent no usable screenshot; tell him it failed"
        raise ToolError(msg, code="screen_capture_failed")
    try:
        image = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        msg = "screen_look: his computer's screenshot was garbled; tell him it failed"
        raise ToolError(msg, code="screen_capture_failed") from exc
    if len(image) > SCREEN_CAPTURE_MAX_IMAGE_BYTES or not image.startswith(_PNG_MAGIC):
        msg = "screen_look: his computer's screenshot was not a usable image; tell him it failed"
        raise ToolError(msg, code="screen_capture_failed")
    return image


def _make_screen_look(
    *, vision_client: VisionClient | None, max_width_px: int, device_link: DeviceLink | None = None,
) -> Tool:
    """Bind the injected vision client + `tools.screen.max_width_px` (ADR-0011 D7).

    With a `device_link` (ADR 0170: this is a brain) the screenshot is taken by the terminal
    and arrives as a PNG; the vision call stays here, so no model key or call ever reaches a
    terminal.
    """

    def screen_look(args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
        question_raw = args.get("question")
        question = (
            question_raw.strip() if isinstance(question_raw, str) and question_raw.strip() else None
        )
        artifacts_dir = ctx.runtime_paths.artifacts_root / SCREEN_ARTIFACTS_DIRNAME
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        # `action_id` makes this collision-free; the timestamp prefix is for chronological `ls`.
        image_path = artifacts_dir / f"{int(ctx.clock.time() * 1000)}_{ctx.action_id}.png"
        if device_link is None:
            _capture_screen(image_path, max_width_px)
        else:
            image_path.write_bytes(_screenshot_from_device(device_link))
        if _screen_is_blank(image_path):
            msg = (
                "screen_look: the screenshot is one flat colour, so the display is probably "
                "asleep or locked; tell him you cannot see the screen until it is awake"
            )
            raise ToolError(msg, code="screen_blank")
        return _describe_screen(vision_client, image_path, question)

    return Tool(
        name="screen_look",
        description=(
            "Take a screenshot of the user's screen and describe what's on "
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


TERMINAL_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    {
        "open_path", "search_notes", "read_file", "read_clipboard", "open_url", "screen_capture",
        "start_night_run", "end_night_run",
    },
)
"""ADR 0170: the tools a terminal can run for its brain.

"""


def make_screen_capture(max_width_px: int) -> Tool:
    """ADR 0170: what a terminal runs for `screen_look`: capture, downscale, return the PNG.

    No vision client and no model call: the brain describes the image with its own key.
    """

    def screen_capture(_args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="jarvis-screen-") as tmp:
            image_path = Path(tmp) / "screen.png"
            _capture_screen(image_path, max_width_px)
            image = image_path.read_bytes()
        if len(image) > SCREEN_CAPTURE_MAX_IMAGE_BYTES:
            msg = (
                f"screen_look: the screenshot is {len(image)} bytes, over the "
                f"{SCREEN_CAPTURE_MAX_IMAGE_BYTES}-byte cap; tell him it failed "
                "(lower tools.screen.max_width_px)"
            )
            raise ToolError(msg, code="screen_image_too_large")
        return {"image_b64": base64.b64encode(image).decode("ascii"), "bytes": len(image)}

    return Tool(
        name="screen_capture",
        description="Capture the main display and return it as a base64 PNG.",
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=screen_capture,
        allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
        risk_level="L1",
        read_only=True,
        max_result_chars=SCREEN_CAPTURE_MAX_IMAGE_BYTES * 4 // 3 + 1024,
    )


# --- ToolRegistry ------------------------------------------------------------


def _leased(action_request: ActionRequest, now_ms: int) -> bool:
    """Whether Allen's lease covers this request's tool and target and outlives ``now_ms``."""
    lease = action_request.authorization_lease
    if lease is None:
        return False
    target = action_request.target_entity_ref
    return (
        lease["granted_by"] == "allen"
        and action_request.tool_name in lease["allowed_tools"]
        and (not lease["allowed_targets"] if target is None else target in lease["allowed_targets"])
        and lease["expires_at_ms"] > now_ms
    )


def _refuse_unleased(_args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
    """The handler a confirm-required call gets when it arrives without a valid lease (I7)."""
    msg = (
        "this tool needs the user's confirmation and the call carried no valid authorization; "
        "it was not run"
    )
    raise ToolError(msg, code="no_lease", kind="dont_retry")


_NOT_A_READ: Final = (
    "this view only reads: that call would change something and was not run; tell him it needs "
    "his own session"
)


def _refuse_not_a_read(_args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
    """The handler a call that changes something gets in a view that only reads."""
    raise ToolError(_NOT_A_READ, code="reads_only_view", kind="impossible")


class ToolRegistry:
    """L4 dispatch surface (ADR § Module map L4 row).

    Owns:
        - The `name → Tool` table.
        - `for_caller(...)` — filtered list the L3 system-prompt assembler
          renders into the LLM tool list.
        - `dispatch(...)` — the L3-facing entry point. Validates caller,
          lifecycle precondition, emits `action.dispatched` +
          `action.running`, calls the handler, returns its `RawResult`.

    Owns NOT:
        - Lifecycle FSM mutation outside `authorized → dispatched → running`
          (the dispatcher writes every tool's terminal).

    Thread-safe by `threading.RLock` around the internal dict; same
    rationale as `ActionLifecycle`.
    """

    def __init__(
        self, *, confirmation_dispatch_outbox: bool = False, clock: Clock | None = None,
        here: HereSource | None = None,
    ) -> None:
        """Construct an empty registry (no tools yet).

        ``clock`` and ``here`` are what every call's context carries (ADR 0322); the runtime
        passes its one clock and its one Here source. A hand-built registry gets the real clock
        and no Here.
        """
        self._clock: Clock = clock or SystemClock()
        self._here = here
        self._tools: dict[str, Tool] = {}
        self._lock = threading.RLock()
        self._confirmation_dispatch_outbox = confirmation_dispatch_outbox
        self._device_catalogue: dict[str, tuple[Tool, str]] = {}
        self._offered_device_tools: frozenset[str] = frozenset()

    @property
    def clock(self) -> Clock:
        """The clock every call's context carries."""
        return self._clock

    def register(self, tool_def: Tool) -> None:
        """Register a Tool. Raises DuplicateToolError on re-register.

        Unlike legacy `tools_v2/registry.py` (which logged and overwrote),
        Day-1 raises so a double-register due to a misconfigured
        composition root is caught at runtime.
        """
        with self._lock:
            if tool_def.name in self._tools:
                msg = f"tool {tool_def.name!r} is already registered"
                raise DuplicateToolError(msg)
            self._tools[tool_def.name] = tool_def

    def get_definitions(self) -> tuple[Tool, ...]:
        """Return every registered tool, in registration order."""
        with self._lock:
            return tuple(self._tools.values())

    def superseded(self) -> dict[str, tuple[str, str]]:
        """``{old name: (new tool, how to call it)}`` for the tools on the menu that replace others.

        A deferred tool replaces nothing: it is not in the model's tools to call instead.
        """
        with self._lock:
            return {
                old: (t.name, how)
                for t in self._tools.values()
                if isinstance(t, Tool) and not t.deferred
                for old, how in t.supersedes.items()
            }

    def replace_group(
        self, previous: frozenset[str], tools: tuple[Tool, ...],
    ) -> None:
        """Atomically publish a runtime-owned group of tools, preserving other tools."""
        with self._lock:
            names = [tool.name for tool in tools]
            if len(set(names)) != len(names) or (set(names) & (self._tools.keys() - previous)):
                msg = "a published tool group collides with another registered tool"
                raise DuplicateToolError(msg)
            self._tools = {
                **{name: tool for name, tool in self._tools.items() if name not in previous},
                **{tool.name: tool for tool in tools},
            }

    def set_device_tools(
        self, catalogue: Mapping[str, tuple[Tool, str]],
    ) -> None:
        """ADR 0225: the device tools a brain may offer, each with the terminal operation it needs.

        Nothing is published until :meth:`offer_device_tools`; a tool already offered is
        withdrawn, so the menu always follows the catalogue it was last given.
        """
        with self._lock:
            self.replace_group(self._offered_device_tools, ())
            self._device_catalogue = dict(catalogue)
            self._offered_device_tools = frozenset()

    def device_catalogue(self) -> tuple[Tool, ...]:
        """Every device tool this registry can offer, offered or not."""
        with self._lock:
            return tuple(tool for tool, _ in self._device_catalogue.values())

    def offer_device_tools(self, declared: frozenset[str]) -> None:
        """ADR 0225: offer exactly the catalogue tools whose operation is in ``declared``.

        A no-op when that set is what is offered already, so the menu (and the prompt prefix
        cached with it) changes only when a terminal's declarations do.
        """
        with self._lock:
            wanted = tuple(tool for tool, op in self._device_catalogue.values() if op in declared)
            names = frozenset(tool.name for tool in wanted)
            if names == self._offered_device_tools:
                return
            self.replace_group(self._offered_device_tools, wanted)
            self._offered_device_tools = names

    def for_caller(self, caller_principal: CallerPrincipal) -> tuple[Tool, ...]:
        """Return only the tools `caller_principal` is allowed to dispatch."""
        with self._lock:
            return tuple(
                t for t in self._tools.values() if caller_principal in t.allowed_callers
            )

    def complete(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        conn: sqlite3.Connection,
        runtime_paths: RuntimePathsLike,
        turn_id: str | None = None,
    ) -> Completed:
        """The full arguments a call that needs a card will run with (ADR 0361).

        A tool with no completion gives its arguments as they are. A completion that raises
        :class:`ToolError` comes back as the failure it names, any other error as an unexpected
        one; it only reads, so nothing was done, and no card follows either.
        """
        with self._lock:
            tool_def = self._tools.get(tool_name)
        if tool_def is None or tool_def.complete is None:
            return Completed(arguments)
        ctx = ToolContext(
            conn, runtime_paths, "", self._clock, None,
            None if self._here is None else self._here.bind(turn_id),
        )
        try:
            return Completed(dict(tool_def.complete(arguments, ctx)))
        except ToolError as exc:
            return Completed(None, str(exc), exc.code, exc.kind)
        except Exception as exc:
            LOGGER.exception("tool %s's completion raised", tool_def.name)
            return Completed(None, _unexpected(tool_def.name, exc), "unexpected_error")

    def dispatch(
        self,
        action_request: ActionRequest,
        conn: sqlite3.Connection,
        runtime_paths: RuntimePathsLike,
        lifecycle: ActionLifecycle,
        *,
        reads_only: bool = False,
    ) -> RawResult:
        """Dispatch one ActionRequest and return its :class:`RawResult`.

        ``reads_only`` is a view that only reads (ADR-0016 D6): a call that changes something
        gets the failure :data:`_NOT_A_READ`, as an unleased one gets L4's refusal.

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
        `jarvis.runtime.turn.drive.drive_turn` releases the turn's entries in a
        `finally`), then emits:
            1. `action.dispatched(action_id)` → lifecycle authorized →
               dispatched. The event_uid is the `source_event_id` of
               the next event.
            2. `action.running(action_id)` → lifecycle dispatched →
               running. The event_uid is stashed so the handler can use
               it as `source_event_id` for `action.result_observed`.

        The handler then runs on the calling thread; the dispatcher turns its
        return or its raised error into the one `RawResult`.
        """
        tool_def = self._checked_tool_def(action_request, lifecycle)
        if reads_only and not only_reads(tool_def, action_request.arguments):
            tool_def = replace(tool_def, handler=_refuse_not_a_read)
        elif call_asks(tool_def, action_request.arguments) and not _leased(
            action_request, int(self._clock.time() * 1000),
        ):
            tool_def = replace(tool_def, handler=_refuse_unleased)  # ADR 0321, I7
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
    ) -> Tool:
        """Validate the three dispatch preconditions and return the definition."""
        with self._lock:
            tool_def = self._tools.get(action_request.tool_name)
        if tool_def is None:
            msg = (
                f"unknown tool: {action_request.tool_name!r}; call only a tool in your list, "
                "or find one with tool_search"
            )
            raise UnknownToolError(msg)

        if action_request.caller_principal not in tool_def.allowed_callers:
            allowed_names = sorted(c.value for c in tool_def.allowed_callers)
            msg = (
                f"tool {tool_def.name!r} is not callable from "
                f"caller_principal={action_request.caller_principal.value!r}; "
                f"allowed: {', '.join(allowed_names) or 'no caller'}"
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
        tool_def: Tool,
        dispatched_event_uid: str,
    ) -> RawResult:
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
            return _observe(
                tool_def, action_request, conn, runtime_paths, lifecycle,
                running_event_uid=running_event.event_uid,
                clock=self._clock, here=self._here,
            )
        finally:
            _clear_running_event_uid(conn, action_request.action_id)


# --- running_event_uid handoff (dispatcher → handler) -----------------------
#
# The handlers need to know the `event_uid` of the `action.running` event
# the dispatcher just emitted, so they can set it as `source_event_id` on
# the events they emit next (`action.result_observed`).
#
# We cannot stash this on `sqlite3.Connection` (it forbids arbitrary
# attribute assignment), and we cannot widen the handler signature
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
# root (`jarvis.runtime.turn.drive.drive_turn`) drops the turn's whole entry in a
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


# --- Default registry assembly ----------------------------------------------

def _runs_on_terminal(_args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
    """ADR 0225: the handler of a device tool a brain builds without its terminal's settings.

    `_device_proxy` replaces it, so it is never run; a brain cannot reach this machine's.
    """
    msg = "this tool runs on a terminal, not here"
    raise RuntimeError(msg)


def _device_proxy(original: Tool, link: DeviceLink) -> Tool:
    """ADR 0170: `original` with its handler swapped for one that runs the call on a terminal.

    Everything the model, the gate and the confirmation read (name, description, schema,
    risk, flags, callers) is `original`'s own; only execution moves. A failed link is the
    tool's plain error result.
    """
    name = original.name

    def on_terminal(args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
        try:
            return link(name, args, ctx.target_entity_ref)
        except DeviceCallError as exc:
            raise ToolError(str(exc), code=exc.code) from exc

    return replace(original, handler=on_terminal)


_OFF_MENU_DAILY: Final[frozenset[str]] = frozenset(
    {"save_briefing", "save_knowledge", "search_knowledge"},
)
"""ADR 0247: the daily-loop tools no turn needs by default; ``tool_search`` finds them."""


def build_default_registry(  # noqa: PLR0913, PLR0915, C901 — every kwarg is a distinct D7 config value threaded into one tool's closure at registry-build time; bundling them into one options object defeats the point of each tool owning its own defaulted knobs.
    *,
    obsidian_vault_root: Path | None = DEFAULT_OBSIDIAN_VAULT_ROOT,
    web_search_max_results: int = DEFAULT_WEB_SEARCH_MAX_RESULTS,
    web_search_provider: str | None = DEFAULT_WEB_SEARCH_PROVIDER,
    web_search_api_key: str | None = None,
    web_fetch_max_bytes: int = DEFAULT_WEB_FETCH_MAX_BYTES,
    web_fetch_max_text_bytes: int = DEFAULT_WEB_FETCH_MAX_TEXT_BYTES,
    web_timeout_s: float = DEFAULT_WEB_TIMEOUT_S,
    web_deep_search: DeepSearch | None = None,
    vision_client: VisionClient | None = None,
    screen_max_width_px: int = DEFAULT_SCREEN_MAX_WIDTH_PX,
    confirmation_dispatch_outbox: bool = False,
    memory_db_path: Path | None = None,
    observed_repos: tuple[str, ...] = (),
    timesink_db_path: Path | None = None,
    work_state_refresh: WorkStateRefresh | None = None,
    daily_report_run: DailyReportRun | None = None,
    night: NightControl | None = None,
    voice_settings: VoiceSettings | None = None,
    mail_drafts: DraftStore | None = None,
    device_link: DeviceLink | None = None,
    weather_lookup: Callable[[WeatherRequest], Mapping[str, Any]] | None = None,
    transit_api_key: str | None = None,
    transit_places: Callable[[], Mapping[str, str]] | None = None,
    transit_offers: TransitOffers | None = None,
    bus_live: BusTimes | None = None,
    ride_control: RideControl | None = None,
    highlight_marker: HighlightMarker | None = None,
    maps_monthly_usd: float = 10.0,
    surroundings: Surroundings | None = None,
    clock: Clock | None = None,
    here: HereSource | None = None,
    plan_day_ends: clock_time | None = None,
    ring_before_min: int = 60,
) -> ToolRegistry:
    """Assemble the default ToolRegistry.

    The composition root (`jarvis.runtime`, Step 10) calls this once at
    startup and passes the registry to L3 + L4.

    Args:
        obsidian_vault_root: Vault root for `search_notes` (ADR-0011
            D7, `tools.obsidian.vault_root`). `None` registers no
            `search_notes`, so nothing touches a folder the user never
            named; a hand-built test registry can point this at a
            `tmp_path` fixture instead. Ignored with a `device_link`: the
            terminal's vault is the one searched (ADR 0225).
        web_search_max_results: `tools.web.search_max_results` (ADR-0011
            D7) — the `web_search` default absent a request argument.
        web_search_provider: `tools.web.search_provider` — which backend
            answers `web_search`; `None` registers no `web_search`. See
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
        web_deep_search: Injected deep-research seam behind `web_search`
            depth deep. `None` (no Anthropic preset on Anthropic's host)
            means a deep request runs the widest quick search instead.
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
        night: ADR 0093 — the runtime's night run; `None` leaves
            `start_night_run` / `end_night_run` off the menu; with a `device_link` they are
            registered as proxies to the terminal, whose run they drive.
        voice_settings: ADR 0174 — her voice volume and speed; `None` leaves
            `set_voice` off the menu.
        mail_drafts: ADR 0147 — the Dashboard's reply drafts; `None` leaves
            `write_mail_draft` off the menu.
        memory_db_path: `memory.db_path` — registers `search_records`
            over that memory.db. `None` (hand-built test registries)
            registers no memory tool.
        device_link: ADR 0170 — the brain's link to its terminal. Every tool that
            acts on the machine it runs on (`open_path`, `search_notes`, `read_file`,
            `read_clipboard`, `open_url`, `screen_look`, the night-run
            tools) is then built, with the same definition, as a proxy that runs the
            call there, and held in the registry's device catalogue; the menu gets the
            ones its terminals declare (ADR 0225, `ToolRegistry.offer_device_tools`).
            `None` (one machine) registers them as they are.
            The activity tools (`query_activity`, `read_activity`, `save_*`) stay as they
            are too, but ask the terminal for what they read of TimeSink and git.
        weather_lookup: ADR 0282 — the weather read for a `WeatherRequest` (home, a named place or
            `here`; the home is `home.weather`); `None` (no location configured) leaves `weather`
            off the menu.
        transit_api_key: ADR 0189 — the Google Routes key (`GOOGLE_MAPS_API_KEY`, resolved
            by the runtime); empty or `None` leaves `bus` unable to read times (and off the
            menu altogether without a `ride_control`).
        transit_places: reads the kept `home` and `school` for `bus` at each call (an
            address or `lat,lng`; ADR 0234).
        clock: ADR 0322 — the one clock every call's context carries, and the zone `places`
            counts days in (`None`: the real clock in the machine's zone).
        here: ADR 0322 — the one source of where he is; gives `bus`, `map_search`, `weather`,
            `save_place` and `where_am_i` their position and registers `where_am_i`. `None`
            leaves `where_am_i` off and those tools unable to read a position.
        plan_day_ends: `day_plan.day_ends`, where `plan` counts the day's time left (`None`: 23:30).
        ring_before_min: `commitments.remind_before_min` — how long before it a thing he goes to
            rings, when `schedule` adds one and `remind` is left out.
        transit_offers: where `bus` leaves its options, for the chat card (ADR 0205) and `pin`.
        bus_live: BC Transit's timetable and realtime feed (`runtime.bus_live.BusLive`), for the
            times `bus` gives and the next buses at a stop.
        ride_control: ADR 0224 — what the runtime does when the model says he is heading out,
            has boarded, gets off at a stop, or is off the bus, and how his ride is going; gives
            `bus` its `ride` and `get_off`. `None` leaves them out of its answers.
        maps_monthly_usd: ADR 0283 — the month's cap on Google map spend (`maps.monthly_usd`);
            `bus` and `map_search` stop near it. 0 or less is no cap.
        surroundings: ADR 0284 — what is around him, held by position; `where_am_i` returns it.
        highlight_marker: ADR 0235 — what the runtime does to mark or unmark an item of a day's
            timeline as a highlight; registers `mark_highlight`. `None` leaves it off the menu.
    """
    registry = ToolRegistry(
        confirmation_dispatch_outbox=confirmation_dispatch_outbox, clock=clock, here=here,
    )
    # ADR 0225: a brain builds every device tool, whatever its own settings say, and keeps them
    # as a catalogue; `offer_device_tools` puts on the menu those its terminals declare.
    device_catalogue: dict[str, tuple[Tool, str]] = {}

    def register_device_tool(device_tool: Tool) -> None:
        if device_link is None:
            registry.register(device_tool)
        else:
            device_catalogue[device_tool.name] = (
                _device_proxy(device_tool, device_link), device_tool.name,
            )

    registry.register(get_current_time)
    register_device_tool(open_path)
    if obsidian_vault_root is not None or device_link is not None:
        register_device_tool(
            _make_search_notes(obsidian_vault_root)
            if obsidian_vault_root is not None
            else _search_notes_tool(_runs_on_terminal)
        )
    register_device_tool(read_file)
    register_device_tool(read_clipboard)
    registry.register(ask_user)
    registry.register(dismiss_card)
    if web_search_provider is not None:
        from jarvis.execution.web_search_tool import make_web_search  # noqa: PLC0415 — same cycle.

        registry.register(
            make_web_search(
                default_max_results=web_search_max_results,
                timeout_s=web_timeout_s,
                provider=web_search_provider,
                api_key=web_search_api_key,
                deep_search=web_deep_search,
            )
        )
    from jarvis.execution.web_fetch_tool import make_web_fetch  # noqa: PLC0415 — same cycle.

    registry.register(
        make_web_fetch(
            max_bytes=web_fetch_max_bytes,
            max_text_chars=web_fetch_max_text_bytes,
            timeout_s=web_timeout_s,
            extract_api_key=(
                web_search_api_key
                if (web_search_provider or "").strip().lower() == "tavily"
                else None
            ),
        )
    )
    register_device_tool(open_url)
    # Already link-aware: the terminal captures, this process describes (ADR 0170).
    screen_look = _make_screen_look(
        vision_client=vision_client, max_width_px=screen_max_width_px, device_link=device_link,
    )
    if device_link is None:
        registry.register(screen_look)
    else:
        device_catalogue[screen_look.name] = (screen_look, "screen_capture")  # ADR 0225
    # Local import avoids a cycle: daily adapters use this module's flat Tool type.
    from jarvis.execution.daily_tools import (  # noqa: PLC0415
        build_daily_report_tool,
        build_daily_tools,
        build_work_state_tool,
    )

    for daily_tool in build_daily_tools(
        memory_db_path, repos=observed_repos, timesink_path=timesink_db_path, device=device_link
    ):
        registry.register(
            replace(daily_tool, deferred=True) if daily_tool.name in _OFF_MENU_DAILY else daily_tool
        )
    for state_tool in build_work_state_tool(work_state_refresh):
        registry.register(state_tool)
    for report_tool in build_daily_report_tool(daily_report_run):
        registry.register(report_tool)
    from jarvis.execution import maps_spend  # noqa: PLC0415 — same cycle.
    from jarvis.execution.bus_tool import build_bus_tool  # noqa: PLC0415 — same cycle.
    from jarvis.execution.highlight_tool import build_highlight_tool  # noqa: PLC0415 — same cycle.
    from jarvis.execution.keep_tool import build_keep_tools  # noqa: PLC0415 — same cycle.
    from jarvis.execution.location_tool import build_where_tool  # noqa: PLC0415 — same cycle.
    from jarvis.execution.mail_draft_tool import build_mail_draft_tool  # noqa: PLC0415
    from jarvis.execution.map_tool import build_map_tool  # noqa: PLC0415 — same cycle.
    from jarvis.execution.night_tools import build_night_tools  # noqa: PLC0415 — same cycle.
    from jarvis.execution.picture_tool import build_picture_tool  # noqa: PLC0415 — same cycle.
    from jarvis.execution.place_reader import PlaceReader  # noqa: PLC0415 — same cycle.
    from jarvis.execution.place_tools import build_place_tools  # noqa: PLC0415 — same cycle.
    from jarvis.execution.places_api import PlaceBook  # noqa: PLC0415 — same cycle.
    from jarvis.execution.plan_tool import build_plan_tool  # noqa: PLC0415 — same cycle.
    from jarvis.execution.schedule_tool import build_schedule_tool  # noqa: PLC0415 — same cycle.
    from jarvis.execution.voice_tools import build_voice_tool  # noqa: PLC0415 — same cycle.
    from jarvis.execution.weather_tool import build_weather_tool  # noqa: PLC0415 — same cycle.

    for night_tool in build_night_tools(night, remote=device_link is not None):
        register_device_tool(night_tool)  # a brain's run is its terminal's
    found_places = PlaceBook()  # what `map_search` found, so `bus` can name a place_id (ADR 0283)
    for conversation_tool in (
        *build_keep_tools(memory_db_path),
        *build_schedule_tool(ring_before_min),
        *build_plan_tool(day_ends=plan_day_ends),
        *build_voice_tool(voice_settings),
        *build_weather_tool(weather_lookup, here),
        *build_picture_tool(vision_client, memory_db_path),
        *(() if memory_db_path is None else build_place_tools(
            memory_db_path,
            PlaceReader(
                transit_api_key, found_places, saved=transit_places or dict,
                kept=lambda: read_kept(memory_db_path),
                before_request=lambda ctx: maps_spend.guard(
                    ctx.conn, maps_monthly_usd, ctx.clock.now(),
                ),
            ),
        )),
        *build_bus_tool(
            transit_api_key, transit_places or dict,
            offers=transit_offers, bus_live=bus_live, control=ride_control,
            kept=None if memory_db_path is None else lambda: read_kept(memory_db_path),
            cap_usd=maps_monthly_usd, book=found_places,
        ),
        *build_map_tool(
            transit_api_key, maps_monthly_usd, found_places, places=transit_places,
            kept=None if memory_db_path is None else lambda: read_kept(memory_db_path),
        ),
        *build_where_tool(here, surroundings),
        *build_highlight_tool(highlight_marker),
    ):
        registry.register(conversation_tool)
    for draft_tool in build_mail_draft_tool(mail_drafts):
        registry.register(draft_tool)
    if device_link is not None:
        registry.set_device_tools(device_catalogue)
    return registry


class ReadOnlyToolRegistry:
    """Read-only view of a :class:`ToolRegistry` for one turn (ADR-0016 D6).

    Exposes the tools with ``read_only=True`` and the tools whose calls differ in effect
    (ADR 0361), whose calls that change something fail with :data:`_NOT_A_READ` and make no card;
    ``dispatch`` of any other tool raises :class:`UnknownToolError` before the inner registry
    emits ``action.dispatched``. The shared registry is not mutated: the composition root hands
    this view to ``decide()`` for turns whose ``surface.user_intent.channel`` is ``gpt_live``.
    """

    def __init__(self, inner: ToolRegistry) -> None:
        """Wrap ``inner``; nothing is copied, filtering happens per call."""
        self._inner = inner

    @staticmethod
    def _visible(tool_def: Tool) -> bool:
        return tool_def.read_only or tool_def.effect is not None

    def get_definitions(self) -> tuple[Tool, ...]:
        """Every tool that can read, in registration order."""
        return tuple(t for t in self._inner.get_definitions() if self._visible(t))

    def for_caller(self, caller_principal: CallerPrincipal) -> tuple[Tool, ...]:
        """The caller's tools narrowed to the ones that can read."""
        return tuple(t for t in self._inner.for_caller(caller_principal) if self._visible(t))

    def _seen(self, tool_name: str) -> Tool | None:
        return next((t for t in self.get_definitions() if t.name == tool_name), None)

    def complete(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        conn: sqlite3.Connection,
        runtime_paths: RuntimePathsLike,
        turn_id: str | None = None,
    ) -> Completed:
        """A call that would ask for a card here fails instead: this view makes no card."""
        tool_def = self._seen(tool_name)
        if tool_def is not None and not only_reads(tool_def, arguments):
            return Completed(None, _NOT_A_READ, "reads_only_view", "impossible")
        return self._inner.complete(tool_name, arguments, conn, runtime_paths, turn_id)

    def dispatch(
        self,
        action_request: ActionRequest,
        conn: sqlite3.Connection,
        runtime_paths: RuntimePathsLike,
        lifecycle: ActionLifecycle,
    ) -> RawResult:
        """Forward a read; a call that changes something fails; any other tool is refused."""
        if self._seen(action_request.tool_name) is None:
            LOGGER.warning(
                "read-only registry refused %r for action %s (ADR-0016 D6)",
                action_request.tool_name, action_request.action_id,
            )
            msg = f"unknown tool: {action_request.tool_name!r} (read-only view)"
            raise UnknownToolError(msg)
        return self._inner.dispatch(
            action_request, conn, runtime_paths, lifecycle, reads_only=True,
        )


__all__ = [
    "DEFAULT_OBSIDIAN_VAULT_ROOT",
    "DEFAULT_SCREEN_MAX_WIDTH_PX",
    "DEFAULT_WEB_FETCH_MAX_BYTES",
    "DEFAULT_WEB_FETCH_MAX_TEXT_BYTES",
    "DEFAULT_WEB_SEARCH_MAX_RESULTS",
    "DEFAULT_WEB_SEARCH_PROVIDER",
    "DEFAULT_WEB_TIMEOUT_S",
    "TERMINAL_TOOL_NAMES",
    "ActionLifecycle",
    "CallerNotAllowedError",
    "DuplicateToolError",
    "IllegalLifecycleTransition",
    "LifecycleState",
    "RawResult",
    "ReadOnlyToolRegistry",
    "ResultSemantics",
    "RuntimePathsLike",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolRegistry",
    "ToolRegistryError",
    "UnknownToolError",
    "VisionClient",
    "build_default_registry",
    "get_current_time",
    "live_action_ids",
    "make_screen_capture",
    "open_path",
    "open_url",
    "read_clipboard",
    "read_file",
    "register_live_action",
    "release_turn_actions",
    "tool",
    "tool_error",
    "tool_result",
    "turn_action_ids",
    "validate_egress_url",
]
