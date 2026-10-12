r"""Tool exam, multi-step mode: each case runs the real decision loop, tools and all.

    .venv/bin/python -m scripts.tool_exam_steps run-steps --cases CASES.json --presets sonnet \
        --out RESULTS.jsonl [--root ~/tool-exam/root-pristine] [--runs 1] [--only id1,id2] \
        [--system-append FILE] [--keep-work] [--fake SCRIPT.json]
    .venv/bin/python -m scripts.tool_exam_steps score-steps RESULTS.jsonl --cases CASES.json

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
A call that needs Allen's confirmation never runs: the turn ends there and the record names the
call ("would have done"). Keys come from the sandbox root's ``env``; none is printed.
"""

from __future__ import annotations

import argparse
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
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import jarvis
import jarvis.runtime as runtime_module
from jarvis.decision import DecideContext, _tool_menu, decide
from jarvis.decision.llm import ChatResult, ToolCall
from jarvis.decision.policy import effective_policy, surface_for
from jarvis.execution.tools import Tool, _device_proxy, release_turn_actions
from jarvis.runtime import _max_tool_iterations, bootstrap_runtime_app
from jarvis.runtime.plugin_catalog import PluginPackage
from jarvis.runtime.plugin_connections import _Active
from jarvis.runtime.plugins import Plugins
from jarvis.shared import CallerPrincipal
from jarvis.shared.pricing import compute_cost_usd, load_pricing_table
from jarvis.state.decision_snapshot import read_decision_snapshot
from jarvis.state.event_log import emit_event
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

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from jarvis.decision.llm import LLMClient
    from jarvis.execution.mcp_tools import McpServers
    from jarvis.execution.tools import ToolDefinition, ToolRegistry
    from jarvis.runtime import JarvisRuntime
    from jarvis.state.decision_snapshot import DecisionFoldState

REPO = Path(__file__).resolve().parent.parent
_DBS = ("mac_events.db", "memory.db")
_RESULT_CHARS = 2000
_LATE_TOOLS = (  # registered after the MCP group
    "job_ledger", "record_application", "mail", "conversation", "my_day",
)

# --- the tool classification (decided per handler; the run refuses an unclassified tool) -------

LIVE_READ = frozenset({
    "get_current_time", "list_memos", "list_reminders", "search_records", "recall", "read_records",
    "query_activity", "read_activity", "search_knowledge", "get_briefing",
    "web_search", "web_fetch", "weather", "transit", "where_am_i", "places",
    "look_at_picture",  # calls the vision preset's model
    "job_ledger", "mail",  # mail() rates unrated unread letters with Jev (OpenRouter)
    "conversation", "my_day",
    "list_plugins", "tool_search", "read_skill",
    "search_notes",  # a device tool: device_not_connected on this host
})
SANDBOX_WRITE = frozenset({
    "create_memo", "set_reminder", "cancel_reminder", "ask_user", "withdraw_card",
    "close_question", "remember", "save_place", "forget_place", "save_knowledge",
    "save_briefing", "record_application", "mark_highlight", "pin_departure",
    "watch_for_boarding", "start_ride", "set_ride_stop", "end_ride",
    "show_on_dashboard",  # its push goes to a recorder
})
STUB = frozenset({
    "open_url", "open_path", "read_file", "write_file", "read_clipboard", "screen_look",
    "start_night_run", "end_night_run", "refresh_work_state", "daily_work_report", "set_voice",
    "write_mail_draft", "open_plugin", "disable_plugin", "remove_plugin",
})
_STUB_NOTE = {"stubbed": True, "note": "sandbox: not run"}
_MICROSOFT_NOTE = {"value": [], "note": "sandbox: calendar not connected"}


def classify(registry: ToolRegistry) -> dict[str, str]:
    """Every registered (or offerable) tool's class; ``SystemExit`` naming any unclassified one."""
    classes, unknown = {}, []
    for tool in (*registry.get_definitions(), *registry.device_catalogue()):
        name = tool.name
        if name in LIVE_READ:
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
    mcp = (runtime.config.get("tools") or {}).get("mcp") or {}
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
    registry = runtime.tool_registry
    with registry._lock:  # noqa: SLF001 - the readers registered after the group stay behind it
        late = {n: registry._tools[n] for n in _LATE_TOOLS if n in registry._tools}  # noqa: SLF001
        registry._tools = {  # noqa: SLF001
            **{n: t for n, t in registry._tools.items() if n not in late}, **late,  # noqa: SLF001
        }


def _stub(tool: Tool | ToolDefinition) -> Tool | ToolDefinition:
    """``tool`` with its handler replaced by a fixed result; schema, risk and flags are kept."""
    note = _MICROSOFT_NOTE if tool.name.startswith("mcp__microsoft__") else _STUB_NOTE
    return _device_proxy(tool, lambda *_: dict(note))


def _apply_stubs(registry: ToolRegistry, classes: Mapping[str, str]) -> None:
    """Replace the stub-class tools in place (menu order kept), the device catalogue included."""
    with registry._lock:  # noqa: SLF001
        registry._tools = {  # noqa: SLF001
            n: _stub(t) if classes.get(n) == "stub" else t
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
    runtime_module._start_locating_places = lambda *_: None  # noqa: SLF001
    runtime = bootstrap_runtime_app(runtime_root=work)
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


# --- the model seam ------------------------------------------------------------------------


class _Substituting:
    """The model client of one case: logged request in, decide()'s own additions after it.

    ``chat`` ignores decide()'s first messages (they were built from the sandbox) and sends the
    logged system and messages, then everything decide() appended since: its tool calls, their
    results and its notes. Every other attribute is the inner client's.
    """

    def __init__(self, inner: Any, system: str, head: list[dict[str, Any]]) -> None:  # noqa: ANN401
        self._inner = inner
        self._system = system
        self._head = head
        self.base: int | None = None
        self.live: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        return getattr(self._inner, name)

    @contextmanager
    def fresh_context(self) -> Iterator[_Substituting]:
        """The loop's marker for an isolated call; this client keeps no history anyway."""
        yield self

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
            sent, sent_system = [*self._head, *messages[self.base:]], self._system
        else:
            sent, sent_system = messages, system
        started = time.monotonic()
        result: ChatResult = self._inner.chat(
            messages=sent, system=sent_system, tools=tools, tool_choice=tool_choice, **extra,
        )
        self.calls.append({
            "result": result, "s": round(time.monotonic() - started, 3), "menu": len(tools or ()),
            "side": not loop,
        })
        return result


class ScriptedClient:
    """A fake model for ``--fake``: each ``chat`` plays the next scripted step, no network.

    A step is ``{"tool_calls": [{"name": ..., "arguments": {...}}], "text": ...}``.
    """

    model = "fake"
    provider = "fake"

    def __init__(self, steps: Sequence[Mapping[str, Any]]) -> None:
        """Hold the script for one case."""
        self._steps = list(steps)
        self._n = 0

    def chat(self, **_kw: Any) -> ChatResult:  # noqa: ANN401
        """The next scripted answer; an exhausted script is an error."""
        if self._n >= len(self._steps):
            msg = "the fake script has no step left for this request"
            raise RuntimeError(msg)
        step = self._steps[self._n]
        self._n += 1
        calls = tuple(
            ToolCall(call_id=f"fake{self._n}_{i}", name=one["name"],
                     arguments_json=json.dumps(one.get("arguments", {})))
            for i, one in enumerate(step.get("tool_calls") or ())
        )
        return ChatResult(
            text=step.get("text"), tool_calls=calls,
            finish_reason="tool_calls" if calls else "stop",
            input_tokens=0, output_tokens=0, raw={}, model_used="fake",
        )


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
    out = []
    for (raw,) in rows:
        payload = json.loads(raw)
        snap = payload.get("action_snapshot") or {}
        out.append({"tool": snap.get("tool_name"), "args": snap.get("args_meta"),
                    "line": payload.get("template_line")})
    return out


def _run_case(  # noqa: PLR0913 - the runtime, its baseline, and the case's model and request.
    runtime: JarvisRuntime, baseline: Path, work: Path, warm: DecisionFoldState | None,
    classes: Mapping[str, str], table: Mapping[str, Any], pushes: list[dict[str, str | None]],
    case: Mapping[str, Any], inner: ScriptedClient | LLMClient, label: str, run: int, extra: str,
) -> dict[str, Any]:
    _restore(runtime, baseline, work)
    pushes.clear()
    system, head = history_of(_with_system(case["request"], extra))
    sub = _Substituting(inner, system, head)
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
        max_tool_iterations=_max_tool_iterations(runtime.config, trigger),
        wave1_features=runtime.wave1_features,
        reply_language=str(runtime.config.get("reply_language", "follow")),
        read_snapshot=partial(read_decision_snapshot, prior=warm),
    )
    started = time.monotonic()
    text, error = "", None
    try:
        plan = decide(trigger, ctx).response_plan
        text = plan.text if plan is not None else ""
    except Exception as exc:  # noqa: BLE001 - one failed case must not stop the exam
        error = f"{type(exc).__name__}: {_SECRET.sub('***', str(exc))[:300]}"
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


def cmd_run_steps(args: argparse.Namespace) -> int:  # noqa: PLR0915 - boot, run each case, report
    """``run-steps``: boot the sandbox runtime, run every case, append the records."""
    pristine = args.root.expanduser().resolve()
    live = (Path.home() / ".jarvis").resolve()
    if pristine == live or live in pristine.parents or not pristine.is_dir():
        msg = f"--root must be a snapshot made by tool_exam_snapshot.py, not {pristine}"
        raise SystemExit(msg)
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    if args.only:
        cases = [c for c in cases if c["id"] in args.only]
    script = json.loads(args.fake.read_text(encoding="utf-8")) if args.fake else None
    extra = args.system_append.read_text(encoding="utf-8").strip() if args.system_append else ""
    table = load_pricing_table(REPO / "data" / "pricing.json")
    work = Path(tempfile.mkdtemp(prefix="root-work-", dir=pristine.parent))
    pushes: list[dict[str, str | None]] = []
    runtime, done = None, 0
    try:
        shutil.copytree(pristine, work, dirs_exist_ok=True)
        runtime = _boot(work, args.cases.parent / "mcp-tools.json", pushes)
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
            for case in cases:
                for preset in presets:
                    for run in range(args.runs):
                        inner = (ScriptedClient(script[case["id"]]) if script is not None
                                 else clients[preset])
                        line = _run_case(runtime, baseline, work, warm, classes, table, pushes,
                                         _for_preset(case, structured, preset), inner, preset,
                                         run, extra)
                        out.write(json.dumps(line, ensure_ascii=False) + "\n")
                        out.flush()
                        done += 1
                        seq = ">".join(c["name"] for s in line["steps"] for c in s["calls"])
                        sys.stderr.write(
                            f"[{done}/{total}] {case['id']} {preset}#{run} -> "
                            f"{line['error'] or seq or 'text'} ({line['wall_s']}s)\n",
                        )
    finally:
        if runtime is not None:
            runtime.conn.close()
            if runtime.plugin_connections is not None:
                runtime.plugin_connections.stop()  # ends the MCP child, so its peak is counted
        if not args.keep_work:
            shutil.rmtree(work, ignore_errors=True)
    summary = _peaks(done)
    with args.out.open("a", encoding="utf-8") as out:
        out.write(json.dumps(summary) + "\n")
    sys.stderr.write(json.dumps(summary) + "\n")
    return 0


# --- scoring -------------------------------------------------------------------------------


def judge_steps(
    spec: Mapping[str, Any], line: Mapping[str, Any], request: Mapping[str, Any],
) -> tuple[bool, str]:
    """Whether one run satisfies ``expect.steps``, and its short label."""
    if line.get("error"):
        return False, f"ERR({str(line['error']).split(':')[0]})"
    calls = [c for s in line["steps"] for c in s["calls"]]
    names = [c["name"] for c in calls]
    why = [f"missing:{n}" for n in spec.get("must_call", ()) if n not in names]
    why += [f"called:{n}" for n in spec.get("must_not_call", ()) if n in names]
    if "max_calls" in spec and len(calls) > spec["max_calls"]:
        why.append(f"calls:{len(calls)}>{spec['max_calls']}")
    if spec.get("no_identical_repeat"):
        seen, _outputs = _history(request)
        for c in calls:
            one = {"name": c["name"], "args": c["args"]}
            if one in seen:
                why.append(f"repeat:{c['name']}")
            seen.append(one)
    text = line.get("text") or ""
    why += ["text~must_not" for rx in spec.get("final_text_must_not", ()) if re.search(rx, text)]
    why += ["text!~must" for rx in spec.get("final_text_must", ()) if not re.search(rx, text)]
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
    cases = {c["id"]: c for c in json.loads(args.cases.read_text(encoding="utf-8"))}
    runs: Runs = {}
    for text in args.results.read_text(encoding="utf-8").splitlines():
        line = json.loads(text) if text.strip() else {}
        if line.get("kind") == "run" and line["case"] in cases:
            runs.setdefault((line["case"], line["preset"]), []).append(line)
    presets = sorted({p for _, p in runs})
    verdicts: Verdicts = {
        key: [judge_steps(spec, line, cases[key[0]]["request"]) for line in sorted(
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
    run.add_argument("--fake", type=Path, help="scripted model: {case id: [step, ...]}")
    run.set_defaults(func=cmd_run_steps)
    score = sub.add_parser("score-steps")
    score.add_argument("results", type=Path)
    score.add_argument("--cases", type=Path, required=True)
    score.set_defaults(func=cmd_score_steps)
    args = parser.parse_args()
    if args.command == "run-steps" and not (args.presets or args.fake):
        parser.error("run-steps needs --presets (or --fake)")
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
