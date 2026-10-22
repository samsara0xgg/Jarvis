r"""Tool exam, multi-step mode: each case runs the real decision loop, tools and all.

    .venv/bin/python -m scripts.tool_exam_steps run-steps --cases CASES.json --presets sonnet \
        --out RESULTS.jsonl [--root ~/tool-exam/root-pristine] [--runs 1] [--only id1,id2] \
        [--system-append FILE] [--keep-work] [--fake SCRIPT.json] [--cap-usd 0.20] \
        [--brain-settings settings.brain.yaml]
    .venv/bin/python -m scripts.tool_exam_steps score-steps RESULTS.jsonl --cases CASES.json
    .venv/bin/python -m scripts.tool_exam_steps quote RESULTS.jsonl --cases CASES.json \
        --preset sonnet [--runs 9] [--margin 0.3] [--only id1,id2]

``--cap-usd`` prices every response's usage (``jarvis.shared.pricing``, ``data/pricing.json``) and
stops before a call that would cross the cap, printing the spend. ``quote`` prices the cases for a
preset from the tokens a previous run measured (a Haiku run's, for Sonnet), plus a margin, so the
owner can be asked before any paid run on the brain model.

``CASES.json`` is the ``tool_exam`` file (``{id, said, request, tags, expect?}``); a case may carry
``expect.steps`` = ``{must_call?, must_not_call?, max_calls?, no_identical_repeat?,
final_text_must_not?, final_text_must?}``: tool names in any order, a bound on proposed calls,
whether a call may repeat one (same name and arguments) already made, and regexes on the final text.

``run-steps`` boots the runtime once on a copy of ``--root`` (made by ``tool_exam_snapshot.py``;
its ``env`` supplies the keys, never the command line), then runs each case through
``decide()`` on the typed channel: the logged request (system, messages) goes to the model as it
was, every later request carries what the loop adds (tool calls and results), and the tools are
this checkout's menu. Both databases are restored to their booted state before every case.
Every registered tool is classed ``live_read`` (real handler, reads only), ``sandbox_write`` (real
handler, writes only inside the sandbox root) or ``stub`` (schema kept, handler replaced by a fixed
"sandbox: not run" result and recorded); the run refuses to start with an unclassified tool.
A ``live_read`` tool whose calls also change something outside the sandbox (``mail``: its archive
changes the mailbox the copied Gmail server reads) answers those calls with the stub result.
A call that needs Allen's confirmation never runs: the turn ends there and the record names the
call ("would have done"). A paid run takes the keys from ``--root``'s ``env`` (the brain's), then
from this machine's saved keys, and stops before any case when a preset's key is missing; none is
printed.
"""

from __future__ import annotations

import argparse
import copy
import itertools
import json
import os
import re
import resource
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
import uuid
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast
from unittest.mock import patch

import yaml

import jarvis
from jarvis.decision import DecideContext, _tool_menu, decide
from jarvis.decision.llm import ChatResult, ToolCall
from jarvis.decision.policy import effective_policy, surface_for
from jarvis.deployment import load_env_file
from jarvis.execution.tools import Tool, _device_proxy, release_turn_actions
from jarvis.runtime.bootstrap import app as boot_module
from jarvis.runtime.plugin_catalog import PluginPackage
from jarvis.runtime.plugin_connections import _Active
from jarvis.runtime.plugins import Plugins
from jarvis.runtime.turn.context import max_tool_iterations
from jarvis.shared import CallerPrincipal
from jarvis.shared.pricing import compute_cost_usd, load_pricing_table
from jarvis.state.decision_snapshot import read_decision_snapshot
from jarvis.state.event_log import emit_event
from jarvis.state.projections import requested_calls
from scripts.replay_decision import _client, history_of
from scripts.tool_exam import (
    _SECRET,
    _history,
    _percentile,
    _with_system,
    shape_report,
    spoken_reply_for,
    structured_presets,
)
from tests.support.runtime_config import boot_runtime

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from jarvis.decision.llm import LLMClient
    from jarvis.execution.mcp_tools import McpServers
    from jarvis.execution.tools import ToolContext, ToolRegistry
    from jarvis.runtime import JarvisRuntime
    from jarvis.state.decision_snapshot import DecisionFoldState

REPO = Path(__file__).resolve().parent.parent
LISTING = "mcp-tools.json"
"""The MCP servers' tool listing beside the cases."""
_DBS = ("mac_events.db", "memory.db")
_RESULT_CHARS = 2000

# --- the tool classification (decided per handler; the run refuses an unclassified tool) -------

LIVE_READ = frozenset({
    "get_current_time", "kept", "search_records", "recall", "read_records",
    "query_activity", "read_activity", "search_knowledge", "get_briefing",
    "web_search", "web_fetch", "weather", "where_am_i", "places",
    "map_search",  # reads Google Places; its spend rows go to the sandbox's log
    "look_at_picture",  # calls the vision preset's model
    "job_ledger",
    "mail",  # reads run; its archive is stubbed (_STUB_ACTIONS), the copy holds his real mailbox
    "conversation", "archive", "my_day",
    "list_plugins", "tool_search", "read_skill",
    "search_notes",  # a device tool: device_not_connected on this host
})
SANDBOX_WRITE = frozenset({
    "keep", "schedule", "plan", "ask_user", "dismiss_card",
    "save_place", "forget_place", "save_knowledge",
    "save_briefing", "record_application", "mark_highlight",
    "bus",  # pins and rides write the sandbox; the times (to=...) read Google Routes LIVE
    "show_on_dashboard",  # its push goes to a recorder
})
STUB = frozenset({
    "open_url", "open_path", "read_file", "read_clipboard", "screen_look",
    "start_night_run", "end_night_run", "refresh_work_state", "daily_work_report", "set_voice",
    "write_mail_draft", "open_plugin", "disable_plugin", "remove_plugin",
    "tidy_day",  # asks the doing lane's model (Opus) and writes a run
    "spawn_worker", "wait_worker", "send_input", "close_worker",  # Codex threads (tools.workers)
})
_STUB_ACTIONS: Final = {"mail": frozenset({"archive"})}
"""The calls of a live tool that must not run: its ``action`` values that change his real mail."""
_STUB_NOTE = {"stubbed": True, "note": "sandbox: not run"}
_MICROSOFT_NOTE = {"value": [], "note": "sandbox: calendar not connected"}


def classify(registry: ToolRegistry, recorded: Collection[str] = ()) -> dict[str, str]:
    """Every registered (or offerable) tool's class; ``SystemExit`` naming any unclassified one.

    A tool of a ``recorded`` MCP server (its listing stands beside the cases) is ``recorded``: the
    runtime's own wrapper runs, and the case's recording answers the call.
    """
    classes, unknown = {}, []
    servers = tuple(f"mcp__{server}__" for server in recorded)
    for tool in (*registry.get_definitions(), *registry.device_catalogue()):
        name = tool.name
        if servers and name.startswith(servers):
            classes[name] = "recorded"
        elif name in LIVE_READ:
            classes[name] = "live_read"
        elif name in SANDBOX_WRITE:
            classes[name] = "sandbox_write"
        elif name in STUB or name.startswith("mcp__microsoft__"):
            classes[name] = "stub"
        elif name.startswith("mcp__gmail__"):  # the copied server: reads run, the rest is stubbed
            classes[name] = "live_read" if tool.read_only else "stub"
        else:
            unknown.append(name)
    if unknown:
        msg = f"unclassified tools, add them to the table in tool_exam_steps.py: {sorted(unknown)}"
        raise SystemExit(msg)
    return classes


# --- the sandbox runtime -------------------------------------------------------------------


class _NoServers:
    """The client of the stand-in Microsoft group: it holds no connection."""

    connected_servers: frozenset[str] = frozenset()

    def stop(self) -> None:
        """Nothing to stop."""


def _microsoft_tools(recorded: Path, always: frozenset[str]) -> tuple[Tool, ...]:
    """Tools with the recorded Microsoft schemas; ``list-``/``get-`` read, the rest ask first."""
    tools = []
    for name, one in json.loads(recorded.read_text(encoding="utf-8")).items():
        if not name.startswith("mcp__microsoft__"):
            continue
        reads = name.split("__", 2)[2].startswith(("list-", "get-"))
        tools.append(Tool(
            name=name, description=one["description"], input_schema=one["input_schema"],
            handler=lambda _args, _ctx: {}, allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L0" if reads else "L3", read_only=reads, requires_confirmation=not reads,
            deferred=name not in always,
        ))
    return tuple(tools)


def _add_microsoft(runtime: JarvisRuntime, recorded: Path) -> None:
    """Publish the stand-in Microsoft group as a connected direct MCP entry would be."""
    conns = runtime.plugin_connections
    mcp = (runtime.config.raw.get("tools") or {}).get("mcp") or {}
    always = frozenset(mcp.get("always_loaded") or ())
    tools = _microsoft_tools(recorded, always)
    if conns is None or not tools:
        return
    key = "mcp-microsoft"
    conns.packages[key] = PluginPackage(key, None, {"name": "microsoft"}, {"microsoft": {}}, 0)
    conns._active[key] = _Active(  # noqa: SLF001
        cast("McpServers", _NoServers()), tools, Plugins(sources={"microsoft": key}),
    )
    conns._status[key] = ("ready", None)  # noqa: SLF001
    conns._publish()  # noqa: SLF001


def _stub(tool: Tool) -> Tool:
    """``tool`` with its handler replaced by a fixed result; schema, risk and flags are kept."""
    note: dict[str, Any] = _STUB_NOTE
    if tool.name.startswith("mcp__microsoft__"):
        note = _MICROSOFT_NOTE
    return _device_proxy(tool, lambda *_: dict(note))


def _guard_actions(tool: Tool) -> Tool:
    """``tool``, but a call whose ``action`` is in :data:`_STUB_ACTIONS` answers with the stub."""
    stubbed = _STUB_ACTIONS.get(tool.name)
    if stubbed is None:
        return tool
    run = tool.handler

    def guarded(args: Mapping[str, Any], ctx: ToolContext) -> Mapping[str, Any]:
        return dict(_STUB_NOTE) if args.get("action") in stubbed else run(args, ctx)

    return replace(tool, handler=guarded)


def _apply_stubs(registry: ToolRegistry, classes: Mapping[str, str]) -> None:
    """Replace the stub-class tools in place (menu order kept), the device catalogue included."""
    with registry._lock:  # noqa: SLF001
        registry._tools = {  # noqa: SLF001
            n: _guard_actions(_stub(t) if classes.get(n) == "stub" else t)
            for n, t in registry._tools.items()  # noqa: SLF001
        }
        registry._device_catalogue = {  # noqa: SLF001
            n: (_stub(t) if classes.get(t.name) == "stub" else t, op)
            for n, (t, op) in registry._device_catalogue.items()  # noqa: SLF001
        }


def _boot(work: Path, recorded: Path, pushes: list[dict[str, str | None]]) -> JarvisRuntime:
    """Boot the runtime on ``work`` (a copy of the sandbox root) and make its tools safe."""
    live = (Path.home() / ".jarvis").resolve()
    work = work.resolve()
    if work == live or live in work.parents:
        msg = f"refusing to boot on {work}: the exam root must not be the live ~/.jarvis"
        raise SystemExit(msg)
    if not Path(jarvis.__file__).resolve().is_relative_to(REPO):
        msg = f"jarvis imported from {jarvis.__file__}, not from the exam checkout {REPO}"
        raise SystemExit(msg)
    os.environ["JARVIS_RUNTIME_ROOT"] = str(work)
    # No geocoding at boot: the places in the copy are located already.
    patch.object(boot_module, "_start_locating_places", lambda *_: None).start()  # for the process
    runtime = boot_runtime(work)
    if runtime.runtime_paths.root != work or runtime.memory is None or not (
        runtime.memory.db_path.is_relative_to(work)
    ):
        msg = f"the runtime booted on {runtime.runtime_paths.root}, not on {work}"
        raise SystemExit(msg)
    if runtime.view is not None:
        runtime.view.push = pushes.append
    if runtime.rides is not None:
        runtime.rides.defer = lambda _fn: None  # a ride's background tick would race the restore
    _add_microsoft(runtime, recorded)
    classes = classify(runtime.tool_registry)
    _apply_stubs(runtime.tool_registry, classes)
    return runtime


def _copy_db(source: Path, target: sqlite3.Connection | Path) -> None:
    """Copy one database into ``target`` (its connection, or a file) with the backup API."""
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = target if isinstance(target, sqlite3.Connection) else sqlite3.connect(target)
    try:
        dst.rollback()
        src.backup(dst)
    finally:
        src.close()
        if dst is not target:
            dst.close()


def _restore(runtime: JarvisRuntime, baseline: Path, work: Path) -> None:
    """Put both databases back as they were after boot, in place (the runtime keeps its conn)."""
    _copy_db(baseline / "mac_events.db", runtime.conn)
    _copy_db(baseline / "memory.db", work / "memory.db")


class SpendCap(Exception):  # noqa: N818 - reads as the fact it states
    """The next call would cross ``--cap-usd`` (or cannot be priced), so the run stops."""


class Meter:
    """The run's spend: each response priced, and a stop before the call that would cross."""

    def __init__(self, cap_usd: float | None, table: Mapping[str, Any]) -> None:
        """Meter against ``cap_usd`` (None: count only) at the ``table``'s rates."""
        self.cap = cap_usd
        self.table = dict(table)
        self.spent = 0.0
        self.calls = 0
        self.last = 0.0
        self.capped = False

    def before(self, model: str, request_chars: int) -> None:
        """Raise :class:`SpendCap` when this call, estimated, would take the spend over the cap.

        The estimate is the larger of the last response's cost and the request priced at
        three characters a token with a short answer; cache hits make a real call cheaper.
        """
        if self.cap is None:
            return
        guess = compute_cost_usd(model, request_chars // 3, _GUESS_OUT, 0, 0, self.table)
        if guess is None:
            self.capped = True
            msg = f"no price for model {model!r} in data/pricing.json: cannot keep to the cap"
            raise SpendCap(msg)
        if self.spent + max(guess, self.last) > self.cap:
            self.capped = True
            msg = (
                f"the next call (about ${max(guess, self.last):.4f}) would take"
                f" ${self.spent:.4f} past ${self.cap:g}"
            )
            raise SpendCap(msg)

    def after(self, result: ChatResult) -> None:
        """Add one response's cost to the spend."""
        cost = compute_cost_usd(
            result.model_used or None, result.input_tokens or 0, result.output_tokens or 0,
            result.cache_read_in, result.cache_write_in, self.table, result.service_tier,
        )
        self.calls += 1
        self.last = cost or 0.0
        self.spent += self.last

    def report(self) -> str:
        """The spend line printed at the end."""
        cap = "" if self.cap is None else f" of ${self.cap:g}"
        return f"spend: ${self.spent:.4f}{cap} over {self.calls} model calls" + (
            " (stopped at the cap)" if self.capped else ""
        )


_GUESS_OUT = 400
"""Tokens of answer assumed when pricing a call before it is made."""


# --- the model seam ------------------------------------------------------------------------


class _Substituting:
    """The model client of one case: logged request in, decide()'s own additions after it.

    ``chat`` ignores decide()'s first messages (they were built from the sandbox) and sends the
    logged system and messages, then everything decide() appended since: its tool calls, their
    results and its notes. Every other attribute is the inner client's.

    With no ``system`` and no ``head`` (a case built at its moment, ``scripts/tool_exam_turns.py``)
    nothing is substituted: the request decide() built goes through as it is, and the first one is
    kept whole as ``first``.
    """

    def __init__(
        self, inner: Any, system: str | None, head: list[dict[str, Any]] | None,  # noqa: ANN401
        meter: Meter | None = None,
    ) -> None:
        self._inner = inner
        self._system = system
        self._head = head
        self._meter = meter
        self.base: int | None = None
        self.live: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.first: dict[str, Any] | None = None

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        return getattr(self._inner, name)

    def chat(
        self, *, messages: list[dict[str, Any]], system: str,
        tools: list[dict[str, Any]] | None = None, tool_choice: str | None = "auto",
        **extra: Any,  # noqa: ANN401
    ) -> ChatResult:
        """Send the logged history plus decide()'s additions; keep the call for the record.

        A request on a list of its own (the spoken-form rewrite of an answer) is decide()'s side
        request and goes through as it is.
        """
        loop = self.base is None or messages is self.live
        if loop:
            self.base = len(messages) if self.base is None else self.base
            self.live = messages
            if self._head is None or self._system is None:
                sent, sent_system = messages, system
                if self.first is None:
                    self.first = copy.deepcopy({"system": system, "messages": messages})
            else:
                sent, sent_system = [*self._head, *messages[self.base:]], self._system
        else:
            sent, sent_system = messages, system
        if self._meter is not None:
            self._meter.before(
                str(getattr(self._inner, "model", "")),
                len(sent_system) + len(json.dumps(sent, default=str))
                + len(json.dumps(tools or ())),
            )
        started = time.monotonic()
        result: ChatResult = self._inner.chat(
            messages=sent, system=sent_system, tools=tools, tool_choice=tool_choice, **extra,
        )
        if self._meter is not None:
            self._meter.after(result)
        self.calls.append({
            "result": result, "s": round(time.monotonic() - started, 3), "menu": len(tools or ()),
            "side": not loop,
        })
        return result


class ScriptedModel:
    """A scripted model for the real ``decide()`` loop: each ``chat`` plays the next step.

    No network. A step is ``{"tool_calls": [{"name": ..., "arguments": {...}}], "text": ...,
    "usage": [in, out]}``.
    A call may give ``arguments_json`` instead: the raw string goes through as it is, so a
    malformed one reaches the loop as a model could send it. Every request is recorded, whole,
    as ``requests``; :meth:`request` renders request ``i`` as chat/completions or as Responses,
    and :meth:`respond` plays a step for a logged request of either shape.
    """

    model = "claude-haiku-5-5"  # a priced id, so a step's zero tokens cost $0 without a warning
    provider = "scripted"
    native_tool_search = False  # Jarvis's own tool_search (ADR 0287)
    last_input_tokens: int | None = 0
    last_output_tokens: int | None = 0
    last_finish_reason: str | None = "stop"
    last_llm_request_id: str | None = None
    last_usage_status = "unavailable"
    last_cache_read_tokens: int | None = 0
    last_cache_write_tokens: int | None = 0
    last_metadata: Mapping[str, Any] = {}

    def __init__(self, steps: Sequence[Mapping[str, Any]]) -> None:
        """Hold the script for one case."""
        self._steps = list(steps)
        self._n = 0
        self.requests: list[dict[str, Any]] = []

    def chat(
        self, *, messages: list[dict[str, Any]], system: str,
        tools: list[dict[str, Any]] | None = None, tool_choice: str | None = "auto",
        **extra: Any,  # noqa: ANN401
    ) -> ChatResult:
        """Record the request as sent (the loop keeps editing ``messages``), play the next step."""
        self.requests.append(copy.deepcopy({
            "system": system, "messages": messages, "tools": tools, "tool_choice": tool_choice,
            "extra": extra,
        }))
        return self._play()

    def respond(self, request: Mapping[str, Any]) -> ChatResult:
        """Play the next step for a logged request in chat/completions or Responses shape."""
        system, messages = history_of(request)
        self.requests.append(copy.deepcopy({
            "system": system, "messages": messages, "tools": request.get("tools"),
            "tool_choice": request.get("tool_choice"), "extra": {},
        }))
        return self._play()

    def request(self, i: int, shape: str = "chat") -> dict[str, Any]:
        """Request ``i`` as it goes on the wire in ``shape`` (``chat`` or ``responses``)."""
        one = self.requests[i]
        if shape == "chat":
            return {
                "messages": [{"role": "system", "content": one["system"]}, *one["messages"]],
                "tools": one["tools"],
            }
        return {
            "instructions": one["system"], "input": _responses_input(one["messages"]),
            "tools": [
                {"type": "function", **t["function"]} if "function" in t else t
                for t in one["tools"] or ()
            ],
        }

    def _play(self) -> ChatResult:
        if self._n >= len(self._steps):
            msg = "the script has no step left for this request"
            raise RuntimeError(msg)
        step = self._steps[self._n]
        self._n += 1
        calls = tuple(
            ToolCall(
                call_id=f"scripted{self._n}_{i}", name=one["name"],
                arguments_json=one["arguments_json"] if "arguments_json" in one
                else json.dumps(one.get("arguments", {})),
            )
            for i, one in enumerate(step.get("tool_calls") or ())
        )
        tokens_in, tokens_out = step.get("usage", (0, 0))
        return ChatResult(
            text=step.get("text"), tool_calls=calls,
            finish_reason="tool_calls" if calls else "stop",
            input_tokens=tokens_in, output_tokens=tokens_out, raw={}, model_used=self.model,
            tokens_in=tokens_in, tokens_out=tokens_out,
        )


ScriptedClient = ScriptedModel
"""The name ``--fake`` used before the model could also play malformed calls."""


def _responses_input(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The loop's chat-shaped messages as Responses ``input`` items."""
    items: list[dict[str, Any]] = []
    for m in messages:
        if m["role"] == "tool":
            items.append({"type": "function_call_output", "call_id": m["tool_call_id"],
                          "output": m["content"]})
            continue
        if m.get("content"):
            items.append({"type": "message", "role": m["role"], "content": m["content"]})
        items += [
            {"type": "function_call", "call_id": c["id"], "name": c["function"]["name"],
             "arguments": c["function"]["arguments"]}
            for c in m.get("tool_calls") or ()
        ]
    return items


# --- one case ------------------------------------------------------------------------------


def _json_or_text(raw: str) -> Any:  # noqa: ANN401
    try:
        return json.loads(raw or "{}")
    except ValueError:
        return raw


def _error_of(content: str) -> str | None:
    one = _json_or_text(content)
    return str(one["error"])[:200] if isinstance(one, dict) and one.get("error") else None


def _record(
    sub: _Substituting, classes: Mapping[str, str], table: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int], float | None]:
    """The model calls as steps (calls, results, usage, time), the summed usage and the cost."""
    results = {
        m["tool_call_id"]: str(m["content"])
        for m in sub.live[sub.base or 0:] if m.get("role") == "tool"
    }
    steps: list[dict[str, Any]] = []
    total = dict.fromkeys(("input", "output", "cache_read", "cache_write"), 0)
    costs: list[float | None] = []
    for one in sub.calls:
        r: ChatResult = one["result"]
        usage = {
            "input": r.input_tokens or 0, "output": r.output_tokens or 0,
            "cache_read": r.cache_read_in, "cache_write": r.cache_write_in,
        }
        for key, value in usage.items():
            total[key] += value
        costs.append(compute_cost_usd(
            r.model_used or None, usage["input"], usage["output"], usage["cache_read"],
            usage["cache_write"], dict(table), r.service_tier,
        ))
        if one["side"]:
            continue  # decide()'s own side request: its usage and cost count, it is no step
        steps.append({
            "i": len(steps) + 1, "s": one["s"], "menu": one["menu"], "text": r.text, "usage": usage,
            "finish": r.finish_reason,
            "calls": [{
                "name": c.name, "args": _json_or_text(c.arguments_json),
                "class": classes.get(c.name, "?"),
                "result": results.get(c.call_id, "")[:_RESULT_CHARS],
                "error": _error_of(results.get(c.call_id, "")),
            } for c in r.tool_calls],
        })
    cost = None if not costs or None in costs else round(sum(cast("list[float]", costs)), 6)
    return steps, total, cost


def _events_after(runtime: JarvisRuntime, after: int) -> list[dict[str, Any]]:
    """The confirmations this turn asked for: the call the model would have made."""
    rows = runtime.conn.execute(
        "SELECT payload_json FROM events WHERE id > ? AND type = 'confirmation.requested' "
        "ORDER BY id", (after,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for (raw,) in rows:
        payload = json.loads(raw)
        out.extend(
            {"tool": snap.get("tool_name"), "args": snap.get("args_meta"),
             "line": payload.get("template_line")}
            for snap in requested_calls(payload)
        )
    return out


def _run_case(  # noqa: PLR0913 - the runtime, its baseline, and the case's model and request.
    runtime: JarvisRuntime, baseline: Path, work: Path, warm: DecisionFoldState | None,
    classes: Mapping[str, str], table: Mapping[str, Any], pushes: list[dict[str, str | None]],
    case: Mapping[str, Any], inner: ScriptedModel | LLMClient, label: str, run: int, extra: str,
    meter: Meter | None = None,
) -> dict[str, Any]:
    _restore(runtime, baseline, work)
    pushes.clear()
    system, head = history_of(_with_system(case["request"], extra))
    sub = _Substituting(inner, system, head, meter)
    turn_id = f"T{uuid.uuid4().hex[:12]}"
    after = int(runtime.conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0])
    trigger = emit_event(
        runtime.conn, type="surface.user_intent",
        payload={"transcript": case["said"], "turn_id": turn_id, "channel": "cli_stdin"},
        correlation={"turn_id": turn_id},
    )
    ctx = DecideContext(
        conn=runtime.conn, runtime_paths=runtime.runtime_paths,
        tool_registry=cast("Any", runtime.tool_registry), lifecycle=cast("Any", runtime.lifecycle),
        llm_client=cast("LLMClient", sub), system_prompt=system,
        max_tool_iterations=max_tool_iterations(runtime.config, trigger),
        wave1_features=runtime.wave1_features,
        reply_language=runtime.config.reply_language,
        read_snapshot=partial(read_decision_snapshot, prior=warm),
    )
    started = time.monotonic()
    text, error = "", None
    try:
        plan = decide(trigger, ctx).response_plan
        text = plan.text if plan is not None else ""
    except Exception as exc:  # noqa: BLE001 - one failed case must not stop the exam
        error = f"{type(exc).__name__}: {_SECRET.sub('***', str(exc))[:300]}"
    if meter is not None and meter.capped:  # decide() may have re-raised it as another error
        error = f"SpendCap: {meter.report()}"
    wall = round(time.monotonic() - started, 3)
    release_turn_actions(turn_id)
    steps, usage, cost = _record(sub, classes, table)
    return {
        "kind": "run", "case": case["id"], "preset": label, "run": run,
        "model": str(getattr(inner, "model", "")), "steps": steps,
        "confirmations": _events_after(runtime, after), "dashboard": list(pushes),
        "text": text, "iterations": len(steps),
        "tool_calls": sum(len(s["calls"]) for s in steps), "wall_s": wall, "usage": usage,
        "cost_usd": cost, "error": error,
    }


def _menu(registry: ToolRegistry) -> list[dict[str, Any]]:
    """The tool menu the decision loop offers first, as ``tool_exam``'s ``run`` rebuilds it."""
    policy = effective_policy({
        who: frozenset(t.name for t in registry.for_caller(who)) for who in CallerPrincipal
    })
    return _tool_menu(surface_for(policy, registry, CallerPrincipal.JARVIS_LLM), ())


def _peaks(cases: int) -> dict[str, Any]:
    """The run's memory peaks: this process, and the largest child (the MCP server)."""
    unit = 1024 if sys.platform != "darwin" else 1024 * 1024  # ru_maxrss: KiB, or bytes on macOS
    return {
        "kind": "summary", "cases": cases,
        "peak_rss_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // unit,
        "peak_child_rss_mb": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss // unit,
    }


def _presets_shape(
    config: Path, presets: Sequence[str], cases: Sequence[Mapping[str, Any]],
) -> dict[str, bool]:
    """Which presets take the structured spoken reply; prints how many cases each rewrites."""
    structured = structured_presets(config, presets)
    sys.stdout.write(shape_report(cases, structured) + "\n")
    return structured


def _for_preset(
    case: Mapping[str, Any], structured: Mapping[str, bool], preset: str,
) -> Mapping[str, Any]:
    """The case with its request in the spoken reply shape of ``preset`` (a fake run has none)."""
    if preset not in structured:
        return case
    return {**case, "request": spoken_reply_for(case["request"], structured=structured[preset])}


def load_cases(path: Path) -> list[dict[str, Any]]:
    """The cases of ``path``: a JSON list, or a directory of one case per ``*.json`` file.

    In a directory, the listing and the recordings beside the cases are not cases.
    """
    if path.is_dir():
        return [
            json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(path.glob("*.json"))
            if p.name != LISTING and not p.name.endswith(".recording.json")
        ]
    return list(json.loads(path.read_text(encoding="utf-8")))


def _load_keys(args: argparse.Namespace) -> None:
    """The brain's keys from the snapshot's ``env``, then this machine's; stop on a missing one.

    Only the variable names are checked here and named in the stop; no value is read.
    """
    root = args.root.expanduser()
    load_env_file(root)
    load_env_file(Path(os.environ.get("JARVIS_RUNTIME_ROOT") or Path.home() / ".jarvis"))
    presets = yaml.safe_load(args.config.read_text(encoding="utf-8"))["llm"]["presets"]
    names = {p: (presets.get(p) or {}).get("api_key_env") for p in args.presets}
    if missing := [f"{p} ({n})" for p, n in names.items() if n and not os.environ.get(n)]:
        msg = (
            f"no key for {', '.join(missing)}: not in the environment, in {root}/env"
            " or among this machine's saved keys"
        )
        raise SystemExit(msg)


def cmd_run_steps(args: argparse.Namespace) -> int:
    """``run-steps``: run every case, logged ones on the snapshot, built ones at their moment."""
    all_cases = load_cases(args.cases)
    if args.only:
        all_cases = [c for c in all_cases if c["id"] in args.only]
    cases = [c for c in all_cases if "request" in c]
    built = [c for c in all_cases if "request" not in c]
    script = json.loads(args.fake.read_text(encoding="utf-8")) if args.fake else None
    extra = args.system_append.read_text(encoding="utf-8").strip() if args.system_append else ""
    table = load_pricing_table(REPO / "data" / "pricing.json")
    pushes: list[dict[str, str | None]] = []
    meter = Meter(args.cap_usd, table)
    done = 0
    if script is None:  # a paid run: the keys, fill-only; the values are never read here
        _load_keys(args)
    if built:
        from scripts.tool_exam_turns import run_built  # noqa: PLC0415 - it imports this module

        done += run_built(built, args, table, meter, script)
    if cases and not meter.capped:
        done += _run_logged(args, cases, script, extra, table, pushes, meter)
    summary = {**_peaks(done), "spend_usd": round(meter.spent, 6), "cap_usd": args.cap_usd,
               "capped": meter.capped}
    with args.out.open("a", encoding="utf-8") as out:
        out.write(json.dumps(summary) + "\n")
    sys.stderr.write(json.dumps(summary) + "\n")
    sys.stdout.write(meter.report() + "\n")
    return 0


def _run_logged(  # noqa: PLR0913 - boot once on the snapshot, run each logged case
    args: argparse.Namespace, cases: list[dict[str, Any]], script: Mapping[str, Any] | None,
    extra: str, table: Mapping[str, Any], pushes: list[dict[str, str | None]], meter: Meter,
) -> int:
    pristine = args.root.expanduser().resolve()
    live = (Path.home() / ".jarvis").resolve()
    if pristine == live or live in pristine.parents or not pristine.is_dir():
        msg = f"--root must be a snapshot made by tool_exam_snapshot.py, not {pristine}"
        raise SystemExit(msg)
    cases_dir = args.cases if args.cases.is_dir() else args.cases.parent
    work = Path(tempfile.mkdtemp(prefix="root-work-", dir=pristine.parent))
    runtime, done = None, 0
    try:
        shutil.copytree(pristine, work, dirs_exist_ok=True)
        runtime = _boot(work, cases_dir / LISTING, pushes)
        classes = classify(runtime.tool_registry)
        baseline = work / "baseline"
        baseline.mkdir()
        runtime.conn.rollback()
        for name in _DBS:
            _copy_db(work / name, baseline / name)
        warm = read_decision_snapshot(runtime.conn).fold_state
        presets = ["fake"] if script is not None else args.presets
        clients = {} if script is not None else {p: _client(args.config, p) for p in presets}
        structured = {} if script is not None else _presets_shape(args.config, presets, cases)
        menu = _menu(runtime.tool_registry)
        sys.stdout.write(
            f"menu: {len(menu)} tools {' '.join(t['name'] for t in menu)}\n"
            f"classes: {json.dumps(classes, sort_keys=True)}\n",
        )
        total = len(cases) * len(presets) * args.runs
        with args.out.open("a", encoding="utf-8") as out:
            out.write(json.dumps({"kind": "menu", "tools": [t["name"] for t in menu],
                                  "classes": classes, "system_append": bool(extra)}) + "\n")
            for case, preset, run in itertools.product(cases, presets, range(args.runs)):
                inner = (ScriptedModel(script[case["id"]]) if script is not None
                         else clients[preset])
                line = _run_case(runtime, baseline, work, warm, classes, table, pushes,
                                 _for_preset(case, structured, preset), inner, preset,
                                 run, extra, meter)
                out.write(json.dumps(line, ensure_ascii=False) + "\n")
                out.flush()
                done += 1
                seq = ">".join(c["name"] for s in line["steps"] for c in s["calls"])
                sys.stderr.write(
                    f"[{done}/{total}] {case['id']} {preset}#{run} -> "
                    f"{line['error'] or seq or 'text'} ({line['wall_s']}s)\n",
                )
                if meter.capped:
                    break
    finally:
        if runtime is not None:
            runtime.conn.close()
            if runtime.plugin_connections is not None:
                runtime.plugin_connections.stop()  # ends the MCP child, so its peak is counted
        if not args.keep_work:
            shutil.rmtree(work, ignore_errors=True)
    return done


# --- scoring -------------------------------------------------------------------------------


def _carries(args: Any, want: Mapping[str, Any]) -> bool:  # noqa: ANN401 - decoded JSON
    """Whether the call's ``args`` hold every pair of ``want``; ``re:`` starts a regex value."""
    if not isinstance(args, dict):
        return False
    for key, value in want.items():
        got = args.get(key)
        if isinstance(value, str) and value.startswith("re:"):
            if got is None or not re.search(value[3:], str(got)):
                return False
        elif got != value and str(got) != str(value):
            return False
    return True


def _arg_failures(spec: Mapping[str, Any], calls: Sequence[Mapping[str, Any]]) -> list[str]:
    """What ``args_must``, ``args_must_not`` and ``min_calls`` ask of the proposed calls."""
    why = []
    for tool, wants in (spec.get("args_must") or {}).items():
        ones = [c["args"] for c in calls if c["name"] == tool]
        if not any(_carries(a, w) for a in ones for w in wants):
            why.append(f"args_must:{tool}")
    for tool, wants in (spec.get("args_must_not") or {}).items():
        ones = [c["args"] for c in calls if c["name"] == tool]
        if any(_carries(a, w) for a in ones for w in wants):
            why.append(f"args_must_not:{tool}")
    for tool, n in (spec.get("min_calls") or {}).items():
        if sum(c["name"] == tool for c in calls) < n:
            why.append(f"min_calls:{tool}<{n}")
    return why


def judge_steps(
    spec: Mapping[str, Any], line: Mapping[str, Any], request: Mapping[str, Any] | None,
) -> tuple[bool, str]:
    """Whether one run satisfies ``expect.steps``, and its short label.

    ``request`` is the logged one; a case built at its moment has none, and
    ``no_identical_repeat`` then looks at this turn's calls alone.
    """
    if line.get("error"):
        return False, f"ERR({str(line['error']).split(':')[0]})"
    calls = [c for s in line["steps"] for c in s["calls"]]
    names = [c["name"] for c in calls]
    why = [f"missing:{n}" for n in spec.get("must_call", ()) if n not in names]
    why += [f"called:{n}" for n in spec.get("must_not_call", ()) if n in names]
    if "max_calls" in spec and len(calls) > spec["max_calls"]:
        why.append(f"calls:{len(calls)}>{spec['max_calls']}")
    if spec.get("no_identical_repeat"):
        seen = _history(request)[0] if request is not None else []
        for c in calls:
            one = {"name": c["name"], "args": c["args"]}
            if one in seen:
                why.append(f"repeat:{c['name']}")
            seen.append(one)
    why += _arg_failures(spec, calls)
    if spec.get("no_card") and line.get("confirmations"):
        why.append("card")
    text = line.get("text") or ""
    why += ["text~must_not" for rx in spec.get("final_text_must_not", ()) if re.search(rx, text)]
    why += ["text!~must" for rx in spec.get("final_text_must", ()) if not re.search(rx, text)]
    for tool, patterns in (spec.get("claims_need_call") or {}).items():
        if tool not in names and any(re.search(rx, text) for rx in patterns):
            why.append(f"claim:{tool}")
    return (not why), ("pass" if not why else f"FAIL({'+'.join(dict.fromkeys(why))})")


def _label(line: Mapping[str, Any], verdict: str) -> str:
    cost = line.get("cost_usd")
    return (f"{verdict} {line['tool_calls']}c/{line['iterations']}i/{line['wall_s']:.1f}s"
            + ("" if cost is None else f"/${cost:.4f}"))


Runs = dict[tuple[str, str], list[dict[str, Any]]]
Verdicts = dict[tuple[str, str], list[tuple[bool, str]]]


def _summary(presets: Sequence[str], runs: Runs, verdicts: Verdicts) -> list[str]:
    out = [f"{'preset':8s} {'passed':>8s} {'calls med':>10s} {'wall med/p90 s':>16s}  cost", ""]
    for preset in presets:
        mine = [line for (_, p), ls in runs.items() if p == preset for line in ls]
        scored = [v for (_, p), v in verdicts.items() if p == preset]
        walls = [line["wall_s"] for line in mine]
        priced = [line["cost_usd"] for line in mine if line.get("cost_usd") is not None]
        out.append(
            f"{preset:8s} {sum(all(ok for ok, _ in v) for v in scored)}/{len(scored):<6d} "
            f"{statistics.median(line['tool_calls'] for line in mine):>10.1f} "
            f"{statistics.median(walls):>7.1f} / {_percentile(walls, 0.9):<6.1f}  "
            f"${sum(priced):.4f} ({len(mine)} runs)",
        )
    return out


def _scored(
    cases: Mapping[str, Any], presets: Sequence[str], runs: Runs, verdicts: Verdicts,
) -> list[str]:
    out = ["", "scored cases:"]
    for case_id, case in cases.items():
        cells = []
        for preset in presets:
            if (case_id, preset) in verdicts:
                lines = sorted(runs[case_id, preset], key=lambda one: one["run"])
                got = verdicts[case_id, preset]
                marks = " ".join(_label(ln, v) for ln, (_, v) in zip(lines, got, strict=True))
                cells.append(f"{preset}[{'PASS' if all(ok for ok, _ in got) else 'FAIL'}]: {marks}")
        if cells:
            out.append(f"  {case_id}  {case['said'][:24]!r}  " + " | ".join(cells))
    return out


def _unscored(cases: Mapping[str, Any], presets: Sequence[str], runs: Runs) -> list[str]:
    out = []
    for case in cases.values():
        if "steps" in (case.get("expect") or {}):
            continue
        cells = [
            f"{p}: " + " | ".join(
                ">".join(c["name"] for s in ln["steps"] for c in s["calls"]) or "text"
                for ln in runs[case["id"], p]) for p in presets if (case["id"], p) in runs
        ]
        if cells:
            out.append(f"  {case['id']}  {case['said'][:24]!r}  " + " | ".join(cells))
    return ["", "unscored (run only, tools called):", *out] if out else []


def cmd_score_steps(args: argparse.Namespace) -> int:
    """``score-steps``: pass/fail per case and preset with calls, iterations, time and cost."""
    cases = {c["id"]: c for c in load_cases(args.cases)}
    runs: Runs = {}
    for text in args.results.read_text(encoding="utf-8").splitlines():
        line = json.loads(text) if text.strip() else {}
        if line.get("kind") == "run" and line["case"] in cases:
            runs.setdefault((line["case"], line["preset"]), []).append(line)
    presets = sorted({p for _, p in runs})
    verdicts: Verdicts = {
        key: [judge_steps(spec, line, cases[key[0]].get("request")) for line in sorted(
            lines, key=lambda one: one["run"])]
        for key, lines in runs.items()
        if (spec := (cases[key[0]].get("expect") or {}).get("steps")) is not None
    }
    out = _summary(presets, runs, verdicts) + _scored(cases, presets, runs, verdicts)
    out += _unscored(cases, presets, runs)
    errors = [ln for ls in runs.values() for ln in ls if ln.get("error")]
    if errors:
        out += ["", "errors (count as failures):"]
        out += [f"  {e['case']} {e['preset']}#{e['run']}: {e['error']}" for e in errors]
    sys.stdout.write("\n".join(out) + "\n")
    return 0


# --- quote ---------------------------------------------------------------------------------


def _usage_cost(
    model: str, usage: Mapping[str, int], table: Mapping[str, Any],
) -> float | None:
    return compute_cost_usd(
        model, usage["input"], usage["output"], usage["cache_read"], usage["cache_write"],
        dict(table),
    )


def run_cost(line: Mapping[str, Any], model: str, table: Mapping[str, Any]) -> float | None:
    """What one measured run would cost on ``model``: each step's tokens as a call, plus the rest.

    The rest is the usage the steps do not hold (decide()'s own side requests), priced as one call.
    None when ``model`` has no price.
    """
    costs = [_usage_cost(model, step["usage"], table) for step in line["steps"]]
    held = {k: sum(step["usage"][k] for step in line["steps"]) for k in line["usage"]}
    rest = {k: max(0, line["usage"][k] - held[k]) for k in line["usage"]}
    if any(rest.values()):
        costs.append(_usage_cost(model, rest, table))
    return None if None in costs else sum(cast("list[float]", costs))


def quote(  # noqa: PLR0913 - the measured runs, the cases, the model and how much to add
    measured: Sequence[Mapping[str, Any]], cases: Sequence[Mapping[str, Any]], model: str,
    table: Mapping[str, Any], *, runs: int, margin: float,
) -> dict[str, Any]:
    """The price of ``runs`` runs of every case on ``model``, from tokens a previous run measured.

    A case with measured runs is priced at their mean; one without is priced at the median of the
    measured cases (``guessed``). ``margin`` is added to every figure (0.3 = 30%) for the other
    model's longer or shorter answers and extra calls.
    """
    by_case: dict[str, list[float]] = {}
    unpriced = 0
    for line in measured:
        if line.get("kind") != "run" or line.get("error"):
            continue
        cost = run_cost(line, model, table)
        if cost is None:
            unpriced += 1
        else:
            by_case.setdefault(line["case"], []).append(cost)
    if unpriced:
        msg = f"{model!r} has no price in data/pricing.json"
        raise SystemExit(msg)
    means = {case: statistics.fmean(costs) for case, costs in by_case.items()}
    if not means:
        msg = "the results hold no finished run to price from"
        raise SystemExit(msg)
    fallback = statistics.median(means.values())
    rows = [
        {"case": c["id"], "usd_per_run": round(means.get(c["id"], fallback) * (1 + margin), 6),
         "guessed": c["id"] not in means}
        for c in cases
    ]
    each = sum(r["usd_per_run"] for r in rows)
    return {"model": model, "runs": runs, "margin": margin, "cases": rows,
            "guessed": sum(r["guessed"] for r in rows), "usd_per_pass": round(each, 4),
            "usd_total": round(each * runs, 4)}


def cmd_quote(args: argparse.Namespace) -> int:
    """``quote``: print what the cases would cost on a preset, priced from a previous run."""
    cases = load_cases(args.cases)
    if args.only:
        cases = [c for c in cases if c["id"] in args.only]
    lines = args.results.read_text(encoding="utf-8").splitlines()
    measured = [json.loads(ln) for ln in lines if ln]
    llm = yaml.safe_load(args.config.read_text(encoding="utf-8"))["llm"]
    model = str(llm["presets"][args.preset]["model"])
    got = quote(measured, cases, model, load_pricing_table(REPO / "data" / "pricing.json"),
                runs=args.runs, margin=args.margin)
    sys.stdout.write(
        f"{len(cases)} cases on {args.preset} ({model}), {args.runs} run(s) each, "
        f"+{args.margin:.0%} margin; {got['guessed']} case(s) have no measured run and are "
        f"priced at the median\n"
        f"one pass ${got['usd_per_pass']:.2f}   total ${got['usd_total']:.2f}\n",
    )
    return 0


def main() -> int:
    """Parse arguments and run the subcommand."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run-steps")
    run.add_argument("--cases", type=Path, required=True)
    run.add_argument("--presets", type=lambda s: s.split(","), default=[])
    run.add_argument("--runs", type=int, default=1)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--only", type=lambda s: s.split(","))
    run.add_argument("--root", type=Path, default=Path("~/tool-exam/root-pristine"))
    run.add_argument("--config", type=Path, default=REPO / "config" / "jarvis.yaml")
    run.add_argument("--system-append", type=Path)
    run.add_argument("--keep-work", action="store_true")
    run.add_argument("--cap-usd", type=float, help="stop before a call that would cross this")
    run.add_argument("--fake", type=Path, help="scripted model: {case id: [step, ...]}")
    run.add_argument(
        "--brain-settings", type=lambda s: Path(s).expanduser(),
        help="the brain's settings.yaml for built cases (default: the cutover-prep copy)",
    )
    run.set_defaults(func=cmd_run_steps)
    score = sub.add_parser("score-steps")
    score.add_argument("results", type=Path)
    score.add_argument("--cases", type=Path, required=True)
    score.set_defaults(func=cmd_score_steps)
    price = sub.add_parser("quote")
    price.add_argument("results", type=Path, help="a previous run-steps file, any preset")
    price.add_argument("--cases", type=Path, required=True)
    price.add_argument("--preset", required=True)
    price.add_argument("--runs", type=int, default=1)
    price.add_argument("--margin", type=float, default=0.3)
    price.add_argument("--only", type=lambda s: s.split(","))
    price.add_argument("--config", type=Path, default=REPO / "config" / "jarvis.yaml")
    price.set_defaults(func=cmd_quote)
    args = parser.parse_args()
    if args.command == "run-steps" and not (args.presets or args.fake):
        parser.error("run-steps needs --presets (or --fake)")
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
