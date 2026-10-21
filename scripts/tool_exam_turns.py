"""Tool exam, whole-turn cases built at their moment (ADR 0317).

Run by ``tool_exam_steps run-steps``.

A case with no ``request`` is built, not replayed:

    {"id", "said", "at", "zone", "state", "recording"?, "tags", "expect": {"steps": {...}}}

``state`` names a full backup under ``~/.jarvis/backups/pi-brain`` (or a fixture directory, like a
tool item's). The runner copies its databases, deletes every row and event made at or after ``at``
(:func:`scripts.tool_exam_tools.cut_at`), freezes the clock at ``at``, boots the runtime on the copy
and sends ``said`` through the runtime's own typed-turn entry (``run_turn``). The code under test
builds the system prompt, the history, the state block and the menu; nothing is read from a log.
``before`` is not supported: the cut leaves the earlier turns in the history as they were.

Providers answer from the case's ``recording`` where it holds the call; a call it lacks fails inside
the tool as a provider error (the network is closed), and the result line lists those calls as
``stubbed_providers``. Tools are classed as in ``tool_exam_steps`` (``live_read``,
``sandbox_write``, ``stub``); ``web_search`` with ``depth`` ``deep`` is a nested paid request and is
always stubbed. A call that needs his confirmation never runs; the record names it.

Configuration is the shipped ``config/jarvis.yaml`` with the brain's ``settings.yaml`` from
``~/.jarvis/backups/cutover-prep`` over it, minus what would reach out or is not about the turn:
no plugin or MCP server (Notion, Gmail, Hue, Linear, Microsoft), no project list, no log of model
input and output, no listen addresses, no spoken streaming (the answer is made whole).
Still unlike the Pi: the plugin tools are absent unless an
``mcp-tools.json`` stands beside the cases, there are no phone, no real-time session and no
background lanes, and ``--presets`` picks the model (the Pi's default is the Settings choice).

The result line is a ``run-steps`` line plus ``built``: the case's moment, the tables the cut could
not handle, the provider calls that were stubbed, and the first 300 characters of the state block
in the model's first request (the clock and the place are the case's).
"""

from __future__ import annotations

import json
import shutil
import signal
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import yaml

from jarvis.decision.llm import ChatResult, LLMClient
from jarvis.execution.tools import Tool
from jarvis.runtime import run_turn
from scripts.replay_decision import _client
from scripts.tool_exam_steps import (
    Meter,
    ScriptedModel,
    SpendCap,
    _add_microsoft,
    _apply_stubs,
    _events_after,
    _record,
    _Substituting,
    classify,
)
from scripts.tool_exam_tools import (
    ITEM_TIMEOUT_S,
    Item,
    Recording,
    _Transport,
    sandbox,
)

if TYPE_CHECKING:
    import argparse
    from collections.abc import Mapping

    from jarvis.execution.tools import FlatHandler, ToolContext
    from jarvis.runtime import JarvisRuntime

BRAIN_SETTINGS = Path("~/.jarvis/backups/cutover-prep/settings.brain.yaml").expanduser()
_NOT_IN_A_TURN = ("projects", "runtime", "diagnostics", "dashboard", "job_mail", "realtime")
_NO_OUTSIDE_SERVERS = ("mcp", "plugins")
STATE_BLOCK_CHARS = 300
_STATE_MARK = "[State when this was said"
_STUB_DEEP = {"stubbed": True, "note": "sandbox: a deep web_search is a nested paid request"}


def brain_settings(directory: Path) -> Path:
    """Write the brain's settings, reduced as the module says, to ``directory``; the directory."""
    data = yaml.safe_load(BRAIN_SETTINGS.read_text(encoding="utf-8")) or {}
    for key in _NOT_IN_A_TURN:
        data.pop(key, None)
    for key in _NO_OUTSIDE_SERVERS:
        (data.get("tools") or {}).pop(key, None)
    (directory / "settings.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    return directory


def _stub_deep_search(runtime: JarvisRuntime) -> None:
    """Make a ``web_search`` call with ``depth`` ``deep`` answer a fixed result."""
    registry = runtime.tool_registry
    for tool in registry.get_definitions():
        if tool.name == "web_search" and isinstance(tool, Tool):
            inner = tool.handler

            def guarded(
                args: Mapping[str, Any], ctx: ToolContext, inner: FlatHandler = inner
            ) -> dict[str, Any]:
                return dict(_STUB_DEEP) if args.get("depth") == "deep" else dict(inner(args, ctx))

            with registry._lock:  # noqa: SLF001
                registry._tools["web_search"] = replace(tool, handler=guarded)  # noqa: SLF001


def state_block(first: Mapping[str, Any] | None) -> str:
    """The first characters of the state block in the model's first request ('' if none found).

    The block opens the user message of the turn it was written for; the last one is this turn's.
    """
    for message in reversed(first["messages"] if first else ()):
        text = str(message.get("content") or "")
        if (at := text.find(_STATE_MARK)) >= 0:
            return text[at : at + STATE_BLOCK_CHARS]
    return ""


def run_one(  # noqa: PLR0913, PLR0915 - the case, its model and what meters and judges it
    case: Mapping[str, Any],
    inner: Any,  # noqa: ANN401 - a client or a scripted model
    label: str,
    run: int,
    base: Path,
    table: Mapping[str, Any],
    meter: Meter,
    preset: str | None,
) -> dict[str, Any]:
    """One built case through ``run_turn``; the result line."""
    rec_name = case.get("recording")
    recording = Recording.load(base / rec_name) if rec_name else Recording()
    transport = _Transport(recording, live=False)
    item = Item(dict(case), base / f"{case['id']}.json")
    scratch = Path(tempfile.mkdtemp(prefix="tool-exam-brain-"))
    started = time.monotonic()
    text, error, built = "", None, {}
    steps: list[dict[str, Any]] = []
    usage = dict.fromkeys(("input", "output", "cache_read", "cache_write"), 0)
    cost: float | None = None
    confirmations: list[dict[str, Any]] = []
    pushes: list[dict[str, str | None]] = []

    def alarm(*_: object) -> None:
        msg = f"timed out after {ITEM_TIMEOUT_S * 5}s"
        raise TimeoutError(msg)

    previous = signal.signal(signal.SIGALRM, alarm)
    signal.alarm(ITEM_TIMEOUT_S * 5)
    try:
        with transport, sandbox(item, recording, real_root=brain_settings(scratch)) as box:
            runtime = box.runtime
            if runtime.view is not None:
                runtime.view.push = pushes.append
            if (base / "mcp-tools.json").is_file():
                _add_microsoft(runtime, base / "mcp-tools.json")
            classes = classify(runtime.tool_registry)
            _apply_stubs(runtime.tool_registry, classes)
            _stub_deep_search(runtime)
            if preset and runtime.llm_session_factory is not None:
                runtime.llm_session_factory.choose(preset)
            sub = _Substituting(inner, None, None, meter)
            after = int(
                runtime.conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
            )
            broke: list[Exception] = []

            def tap(_self: LLMClient, **kw: Any) -> ChatResult:  # noqa: ANN401
                """The model call; its failure is kept, the cost guard would mask it."""
                try:
                    return sub.chat(**kw)
                except Exception as exc:
                    broke.append(exc)
                    raise

            try:
                with patch.object(LLMClient, "chat", tap):
                    text = run_turn(
                        runtime, utterance=case["said"], trigger_timeout_s=5.0
                    ).response_plan.text
            except Exception as exc:  # noqa: BLE001 - one failed case must not stop the exam
                cause = broke[0] if broke else exc
                error = f"{type(cause).__name__}: {str(cause)[:300]}"
            if meter.capped:
                error = f"SpendCap: {meter.report()}"
            steps, usage, cost = _record(sub, classes, table)
            confirmations = _events_after(runtime, after)
            built = {
                "at": case["at"],
                "state": case.get("state"),
                "uncut": box.uncut,
                "state_block": state_block(sub.first),
            }
    except (LookupError, RuntimeError, TimeoutError, SpendCap) as exc:
        error = error or f"{type(exc).__name__}: {str(exc)[:300]}"
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        shutil.rmtree(scratch, ignore_errors=True)
    built["stubbed_providers"] = transport.misses
    return {
        "kind": "run",
        "case": case["id"],
        "preset": label,
        "run": run,
        "model": str(getattr(inner, "model", "")),
        "steps": steps,
        "confirmations": confirmations,
        "dashboard": pushes,
        "text": text,
        "iterations": len(steps),
        "tool_calls": sum(len(s["calls"]) for s in steps),
        "wall_s": round(time.monotonic() - started, 3),
        "usage": usage,
        "cost_usd": cost,
        "error": error,
        "built": built,
    }


def run_built(
    cases: list[dict[str, Any]],
    args: argparse.Namespace,
    table: Mapping[str, Any],
    meter: Meter,
    script: Mapping[str, Any] | None,
) -> int:
    """Run every built case for every preset and run; append the lines to ``--out``."""
    base = args.cases if args.cases.is_dir() else args.cases.parent
    presets = ["fake"] if script is not None else args.presets
    done = 0
    with args.out.open("a", encoding="utf-8") as out:
        for case in cases:
            for preset in presets:
                for run in range(args.runs):
                    inner = (
                        ScriptedModel(script[case["id"]])
                        if script is not None
                        else _client(args.config, preset)
                    )
                    line = run_one(
                        case,
                        inner,
                        preset,
                        run,
                        base,
                        table,
                        meter,
                        None if script is not None else preset,
                    )
                    out.write(json.dumps(line, ensure_ascii=False) + "\n")
                    out.flush()
                    done += 1
                    b = line["built"]
                    seq = ">".join(c["name"] for s in line["steps"] for c in s["calls"])
                    sys.stdout.write(
                        f"{case['id']} {preset}#{run}: {line['error'] or seq or 'text'}\n"
                        f"  cut left these tables as they were: "
                        f"{', '.join(b.get('uncut', [])) or 'none'}\n"
                        f"  stubbed provider calls: {len(b['stubbed_providers'])}\n"
                        f"  state block: {b.get('state_block', '')!r}\n",
                    )
                    if meter.capped:
                        return done
    return done
