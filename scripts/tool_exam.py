r"""Tool exam: replay logged decision requests and score whether the FIRST step uses the right tool.

    .venv/bin/python -m scripts.tool_exam run --cases CASES.json --presets haiku,luna,sonnet \
        --runs 3 --out RESULTS.jsonl [--only id1,id2] [--system-append FILE] [--concurrency 6]
    .venv/bin/python -m scripts.tool_exam score RESULTS.jsonl --cases CASES.json

``CASES.json`` is private conversation data and lives outside the repo
(``~/.jarvis/evals/tool-exam/cases.json``): a list of ``{id, said, request, tags, expect?}`` where
``request`` is a logged decision request (``kind: decision``, Responses or chat shape) and
``expect.first`` is ``{ok, bad, text_must_not?, args_must_not?, args_must?, no_identical_repeat?,
cursor_must_come_from_history?, must_mention?, max_calls?}``: ``ok`` names tools (or ``"text"``, a
spoken answer
with no tool) that pass, ``bad`` names fail even next to an ok one, ``text_must_not`` regexes fail a
no-tool answer, ``args_must_not`` maps a tool to substrings its raw arguments must not hold, or to
an ``{arg: value}`` dict that fails a call carrying all of those values (``"*"``: any value),
``args_must`` maps a tool to ``{arg: value}`` dicts of which a call must carry one (how a merged
tool's ok use is told from its bad one), ``no_identical_repeat`` fails a call the request's history
already made with the same arguments, ``cursor_must_come_from_history`` fails a ``cursor`` no
earlier tool output gave, and ``must_mention`` regexes (one must match the answer or the call
arguments) check that misheard words were understood, and ``max_calls`` fails a step that
proposes more calls than that. Next to it sits ``mcp-tools.json``: the MCP
tool definitions (``{mcp__server__tool: {name, description, input_schema}}``) a live server would
list, which an offline run cannot ask for. An optional ``tool-aliases.json`` next to the cases maps
a tool name the cases still use to the name that replaced it (``{"mail_inbox": "mail"}``): ``score``
reads the names in ``ok``, ``bad`` and ``args_must_not`` through it, so a merged tool is judged
without editing the cases. The file is shared by runs made on different checkouts, so an alias
applies to a run only when its replacement is in the tools of the ``menu`` line that precedes that
run in the results file (a run with no menu line before it is read through every alias). An
``args_must_not`` rule keeps the old tool's argument names, so it trips only on a call that still
uses them. An optional ``prompt-patches.json`` (``[{old, new}]``)
rewrites the logged system prompt the way this checkout's prompt reads (see :func:`patched`).

``run --guards`` applies the decision loop's two turn guards (ADR 0240, the same code) to the first
response: a call identical to an earlier one in the request's history (with no write since) gets
the repeat note as its result, or an answer no call backs gets the program's note; either way one
more request follows, exactly as the runtime would send it, and that one is the step judged. The
result line keeps both steps (``guarded``: kind, first calls, first text, first usage) and
``score`` marks such runs ``(g)``. A response that mixes repeats with a call that would run is
left unguarded: that call's result is not known here.

A logged request is sent as production would build it for each preset (ADR 0241): a preset that
says ``spoken_structured: false`` gets the unstructured spoken note and no text format, any other
preset the structured note and the schema; the note is swapped in the request's instructions, so
a logged request whose note is an older text is left as logged (``run`` prints how many of each).
The switch ``realtime.response.spoken_streaming.structured`` is taken as on, as it was when the
cases were logged.

``run`` sends each case through ``LLMClient.stream_events`` as ``replay_decision`` does, but with
the tool menu THIS checkout would offer the decision model (see :func:`offered_tools`), not the
tools the log recorded, so a merged or renamed tool is exam-visible at once. Keys come from the
environment, then the runtime root's saved keys; none is printed. Results are appended to
``--out``, one JSON line per case x preset x run after one ``menu`` line. Run 0 of every case goes
first and a case's later runs wait for it, so they read the prompt cache it wrote.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import statistics
import sys
import tempfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO
from unittest.mock import MagicMock

import yaml

from jarvis.decision import (
    _SPOKEN_REPLY_NOTE,
    _SPOKEN_REPLY_NOTE_STRUCTURED,
    SPOKEN_REPLY_FORMAT,
    _tool_menu,
)
from jarvis.decision.llm_session import LLMSessionFactory
from jarvis.decision.policy import effective_policy, surface_for
from jarvis.decision.turn_guards import (
    TurnCall,
    calendar_in_view,
    call_key,
    repeated_call,
    ride_stop_in_view,
    unbacked_claim,
)
from jarvis.deployment import load_env_file
from jarvis.execution.conversation_tool import build_conversation_tool
from jarvis.execution.dashboard_tool import build_dashboard_tool
from jarvis.execution.job_ledger_tool import build_job_ledger_tool, build_record_application_tool
from jarvis.execution.mail_tool import build_mail_tool
from jarvis.execution.my_day_tool import build_my_day_tool
from jarvis.execution.plugin_panel import plugin_panel_tools
from jarvis.execution.schedule_tool import build_schedule_tool
from jarvis.execution.tool_search import build_tool_search
from jarvis.execution.tools import Tool, ToolContext, ToolRegistry, build_default_registry
from jarvis.runtime.dashboard import CLOSE, PAGES, SETTINGS_CATEGORIES, ViewState
from jarvis.runtime.plugin_catalog import read_package
from jarvis.shared import CallerPrincipal
from jarvis.shared.pricing import compute_cost_usd, load_pricing_table
from jarvis.state.ledger import LedgerSources
from scripts.replay_decision import _client, replay_once

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping, Sequence

    from jarvis.decision.llm import LLMClient

REPO = Path(__file__).resolve().parent.parent
_SECRET = re.compile(r"(sk-|key-|Bearer\s)[\w.-]{8,}")
_P90 = 0.9


def _unreachable(_args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
    msg = "tool_exam offers tools to the model; it never runs one"
    raise RuntimeError(msg)


def _mcp_tools(recorded: Path, always_loaded: frozenset[str]) -> list[Tool]:
    """Tool stand-ins for the recorded MCP definitions; ``deferred`` follows the config."""
    return [
        Tool(
            name=name, description=one["description"], input_schema=one["input_schema"],
            handler=_unreachable, allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L0", read_only=True, deferred=name not in always_loaded,
        )
        for name, one in json.loads(recorded.read_text(encoding="utf-8")).items()
    ]


def _plugin_sources() -> dict[str, str]:
    """Server -> its repo plugin's description, as ``PluginConnections`` hands ``tool_search``."""
    sources: dict[str, str] = {}
    for directory in sorted((REPO / "plugins").iterdir()):
        try:
            package = read_package(directory)
        except (OSError, ValueError, TypeError):
            continue
        for server in package.servers:
            sources[server] = str(package.manifest.get("description") or package.plugin_id)
    return sources


def offered_tools(config: Mapping[str, Any], recorded_mcp: Path) -> list[dict[str, Any]]:
    """The tool menu this checkout offers the decision model, rebuilt offline (see the registry)."""
    return _menu(_exam_registry(config, recorded_mcp))


def _exam_registry(config: Mapping[str, Any], recorded_mcp: Path) -> ToolRegistry:
    """The registry behind that menu.

    The same registry the runtime assembles (``build_default_registry`` with a brain's device
    tools offered, then ``show_on_dashboard``, the plugin panel, the MCP tools with
    ``tool_search`` over the deferred ones, the job, mail and schedule readers), every dependency a
    harmless stand-in, then the decision loop's own filter and projection (``surface_for`` and
    ``_tool_menu`` over the Collaborate policy). Registration order is the runtime's, since the
    menu's order is part of the request.
    """
    tools = config.get("tools") or {}
    always = frozenset((tools.get("mcp") or {}).get("always_loaded") or ())
    stub: Any = MagicMock()
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        registry = build_default_registry(
            obsidian_vault_root=base, web_search_provider="tavily", web_search_api_key="x",
            vision_client=stub, memory_db_path=base / "memory.db",
            timesink_db_path=base / "timesink.db", work_state_refresh=stub, night=stub,
            voice_settings=stub, mail_drafts=stub, weather_lookup=lambda _request: {},
            transit_api_key="x",
            transit_places=lambda: {"home": "home"}, transit_offers=stub, here_location=dict,
            here_phone=dict, bus_live=lambda *_: None, ride_control=stub, highlight_marker=stub,
            device_link=lambda *_: {},
        )
    # A brain's terminals declare every operation here (ADR 0225): offer the whole catalogue.
    registry.offer_device_tools(frozenset(op for _, op in registry._device_catalogue.values()))  # noqa: SLF001
    group = _mcp_tools(recorded_mcp, always)
    sources = _plugin_sources()
    group.sort(key=lambda one: one.name.split("__")[1] not in sources)  # plugin servers load first
    servers = {one.name.split("__")[1] for one in group}
    for tool in (
        *build_dashboard_tool((*PAGES, CLOSE), ViewState().present, SETTINGS_CATEGORIES),
        *plugin_panel_tools(dict, lambda *_: {}, lambda *_: {}, lambda *_: {}),
        *build_job_ledger_tool(dict), *build_record_application_tool(lambda _fields: ""),
        *build_mail_tool(dict, lambda *_: {}, str, UTC),
        *build_conversation_tool(Path("memory.db"), UTC),
        *build_my_day_tool(LedgerSources(Path("memory.db"), Path("events.db"), None, UTC)),
        *build_schedule_tool(lambda *_: [], UTC),
    ):
        registry.register(tool)
    # The runtime publishes the MCP group and tool_search once every built-in tool is registered
    # (`PluginConnections.refresh_search`); tool_search also finds the built-ins kept off the menu.
    taken = registry.superseded()
    off_menu = tuple(
        t for t in registry.get_definitions()
        if isinstance(t, Tool) and t.deferred and t.name not in taken
    )
    for tool in (
        *group,
        *build_tool_search(
            (*group, *off_menu), {s: sources.get(s, f"mcp-{s}") for s in servers},
            registry.superseded,
        ),
    ):
        registry.register(tool)
    return registry


def _menu(registry: ToolRegistry, *, native: bool = False) -> list[dict[str, Any]]:
    """The first request's tools; ``native``: as Anthropic's host gets them (ADR 0287)."""
    policy = effective_policy({
        who: frozenset(t.name for t in registry.for_caller(who)) for who in CallerPrincipal
    })
    return _tool_menu(surface_for(policy, registry, CallerPrincipal.JARVIS_LLM), (), native=native)


def _menu_chars(menu: Sequence[Mapping[str, Any]]) -> int:
    """What the first request carries of the menu: a deferred tool's text waits for a search."""
    return sum(
        len(t["description"]) + len(json.dumps(t["input_schema"]))
        for t in menu if "type" not in t and not t.get("defer_loading")
    )


def _with_system(request: Mapping[str, Any], extra: str) -> dict[str, Any]:
    """``request`` with ``extra`` appended to its instructions (Responses) or system message."""
    out = dict(request)
    if not extra:
        return out
    if "input" in request:
        out["instructions"] = f"{request.get('instructions') or ''}\n\n{extra}"
    else:
        first, *rest = request["messages"]
        out["messages"] = [{**first, "content": f"{first['content']}\n\n{extra}"}, *rest]
    return out


def structured_presets(config: Path, presets: Sequence[str]) -> dict[str, bool]:
    """Per preset: whether production gives its spoken turns the structured reply (ADR 0241)."""
    factory = LLMSessionFactory(yaml.safe_load(config.read_text(encoding="utf-8"))["llm"])
    return {name: factory.snapshot(name).spoken_structured for name in presets}


_NOTE_STARTS = (
    'Your reply is a JSON object with "spoken"', "Your reply is spoken aloud as you write it.",
)
"""How either spoken reply note begins, in every version the logged requests carry."""


def spoken_reply_for(request: Mapping[str, Any], *, structured: bool) -> dict[str, Any]:
    """``request`` with the spoken reply shape a preset gets: the note, and the text format.

    The note in the instructions, of either shape and any older wording (its paragraph, found by
    how it begins), becomes this shape's; the schema goes (unstructured) or comes (structured). A
    request without a note is unchanged.
    """
    out = dict(request)
    text = request.get("instructions") or ""
    at = min((i for one in _NOTE_STARTS if (i := text.find(one)) >= 0), default=-1)
    if at < 0:
        return out
    end = text.find("\n\n", at)
    end = len(text) if end < 0 else end
    note = _SPOKEN_REPLY_NOTE_STRUCTURED if structured else _SPOKEN_REPLY_NOTE
    out["instructions"] = text[:at] + note + text[end:]
    if structured:
        out["text"] = {"format": SPOKEN_REPLY_FORMAT}
    else:
        out.pop("text", None)  # production's ``text`` holds the format and nothing else
    return out


def shape_report(cases: Sequence[Mapping[str, Any]], structured: Mapping[str, bool]) -> str:
    """One line: per preset, how many cases :func:`spoken_reply_for` rewrote."""
    parts = []
    for preset, flag in structured.items():
        shaped = [spoken_reply_for(c["request"], structured=flag) for c in cases]
        changed = sum(one != c["request"] for one, c in zip(shaped, cases, strict=True))
        left = 0 if flag else sum(bool((one.get("text") or {}).get("format")) for one in shaped)
        parts.append(
            f"{preset} {'structured' if flag else 'unstructured'}: "
            f"{changed}/{len(cases)} rewritten"
            + (f", {left} still carry the schema (their note is an older text)" if left else ""),
        )
    return "spoken reply shape: " + "; ".join(parts)


def _calls(raw: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    calls = []
    for call in raw:
        try:
            args: Any = json.loads(call["arguments"] or "{}")
        except ValueError:
            args = call["arguments"]
        calls.append({"name": call["name"], "args": args})
    return calls


async def _step(
    client: LLMClient, request: Mapping[str, Any], catalog: Mapping[str, Any],
    base: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        got = await replay_once(client, request, catalog)
    except Exception as exc:  # noqa: BLE001 - one failed request must not stop the exam
        return {**base, "error": f"{type(exc).__name__}: {_SECRET.sub('***', str(exc))[:300]}"}
    return {
        **base, "first_token_s": got["first_token_s"], "total_s": got["total_s"],
        "text": got["text"], "calls": _calls(got["tool_calls"]),
        "finish_reason": got["finish_reason"], "usage": got["usage"], "error": got["error"],
    }


_LANGUAGE_LINE = re.compile(r"\[Reply language for this turn: [^\]]+\]")


_STATE_HEADER = re.compile(r"\[(?:Current state|State when this was said) \| from the program")


def _state_block(request: Mapping[str, Any]) -> str:
    """The text of the request's last user message that carries a state block, else "".

    That is the turn's own block: earlier ones replayed in the history come before it. A
    request logged before the calendar line existed has none, so no calendar claim is backed.
    """
    for item in reversed(request["input"] if "input" in request else request["messages"]):
        content = item.get("content") if item.get("role") == "user" else None
        text = content if isinstance(content, str) else "\n".join(
            str(part.get("text", "")) for part in content or () if isinstance(part, dict)
        )
        if _STATE_HEADER.search(text):
            return text
    return ""


def _key(args: Any) -> str:  # noqa: ANN401 - a parsed argument object, or the raw text of one
    return call_key(args) if isinstance(args, dict) else str(args)


def _failed(output: str) -> bool:
    try:
        body = json.loads(output)
    except ValueError:
        return False
    return isinstance(body, dict) and "error" in body


def _turn_calls(request: Mapping[str, Any], read_only: Mapping[str, bool]) -> list[TurnCall]:
    """The request's earlier calls as the decision loop records them (an unknown tool may write)."""
    earlier, outputs = _history(request)
    return [
        TurnCall(c["name"], read_only.get(c["name"], False), not _failed(out), _key(c["args"]))
        for c, out in zip(earlier, outputs, strict=False)
    ]


def _continued(
    request: Mapping[str, Any], text: str, calls: Sequence[Mapping[str, Any]],
    outputs: Sequence[str], note: str | None,
) -> dict[str, Any]:
    """``request`` plus the model's turn and what the loop appends after it (shapes as logged).

    The loop ends the next request with the turn's reply-language line (ADR 0135), which the
    request already holds from its last tool batch or its user message.
    """
    ids = [f"call_guard{i}" for i in range(len(calls))]
    extra = [{"role": "user", "content": line} for line in (note,) if line]
    language = _LANGUAGE_LINE.findall(json.dumps(request, ensure_ascii=False))
    if language:
        extra.append({"role": "user", "content": language[-1]})
    args = [json.dumps(c["args"], ensure_ascii=False) if not isinstance(c["args"], str)
            else c["args"] for c in calls]
    if "input" in request:  # Responses
        items: list[dict[str, Any]] = [{"role": "assistant", "content": text}] if text else []
        items += [{"type": "function_call", "call_id": i, "name": c["name"], "arguments": a}
                  for i, c, a in zip(ids, calls, args, strict=True)]
        items += [{"type": "function_call_output", "call_id": i, "output": o}
                  for i, o in zip(ids, outputs, strict=True)]
        return {**request, "input": [*request["input"], *items, *extra]}
    turn: list[dict[str, Any]] = [{"role": "assistant", "content": text}]  # chat
    if calls:
        turn = [{"role": "assistant", "content": text, "tool_calls": [
            {"id": i, "type": "function", "function": {"name": c["name"], "arguments": a}}
            for i, c, a in zip(ids, calls, args, strict=True)
        ]}, *({"role": "tool", "tool_call_id": i, "content": o}
              for i, o in zip(ids, outputs, strict=True))]
    return {**request, "messages": [*request["messages"], *turn, *extra]}


def _guard(
    request: Mapping[str, Any], line: Mapping[str, Any], read_only: Mapping[str, bool],
) -> tuple[str, dict[str, Any]] | None:
    """The guard the loop would apply to a first response, and the request that follows it."""
    turn = _turn_calls(request, read_only)
    calls, text = line["calls"], line["text"] or ""
    if calls:
        notes = [repeated_call(turn, c["name"], _key(c["args"])) for c in calls]
        if all(notes):  # one that would run has a result this exam cannot give
            return "repeat", _continued(request, text, calls, [str(n) for n in notes], None)
        return None
    state = _state_block(request)
    note = unbacked_claim(
        text, turn, calendar_seen=calendar_in_view(state), ride_stop_seen=ride_stop_in_view(state),
    )
    return None if note is None else ("unbacked", _continued(request, text, [], [], note))


async def _one(
    client: LLMClient, request: Mapping[str, Any], catalog: Mapping[str, Any],
    base: Mapping[str, Any], read_only: Mapping[str, bool] | None,
) -> dict[str, Any]:
    """One run: its first step, then the guard's follow-up when ``--guards`` gave ``read_only``."""
    first = await _step(client, request, catalog, base)
    guard = None if read_only is None or first["error"] else _guard(request, first, read_only)
    if guard is None:
        return first
    kind, followed = guard
    final = await _step(client, followed, catalog, base)
    # The user hears the first request's first token; the answer costs both requests' time.
    timed = {} if final["error"] else {
        "first_token_s": first["first_token_s"],
        "total_s": round(first["total_s"] + final["total_s"], 3),
    }
    return {**final, **timed, "guarded": {
        "kind": kind, "first_calls": first["calls"], "first_text": first["text"],
        "first_usage": first["usage"],
    }}


async def _run(
    args: argparse.Namespace, menus: Mapping[bool, list[dict[str, Any]]],
    read_only: Mapping[str, bool] | None,
) -> None:
    config = args.config
    clients = {name: _client(config, name) for name in args.presets}
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    if args.only:
        cases = [c for c in cases if c["id"] in args.only]
    # A preset on Anthropic's host gets the menu its own tool search reads (ADR 0287).
    catalogs = {
        name: {t["name"]: t for t in menus[client.native_tool_search]}
        for name, client in clients.items()
    }
    extra = args.system_append.read_text(encoding="utf-8").strip() if args.system_append else ""
    structured = structured_presets(config, args.presets)
    sys.stdout.write(shape_report(cases, structured) + "\n")
    patches = load_patches(args.cases)
    hits = sum(patched(c["request"], patches) != c["request"] for c in cases)
    sys.stdout.write(f"prompt patches: {len(patches)}, applied to {hits}/{len(cases)} cases\n")
    sys.stdout.flush()
    gate = asyncio.Semaphore(args.concurrency)
    total = len(cases) * len(clients) * args.runs
    done = 0

    async def job(case: dict[str, Any], preset: str, run: int, out: TextIO) -> None:
        nonlocal done
        shaped = spoken_reply_for(patched(case["request"], patches), structured=structured[preset])
        request = {**_with_system(shaped, extra), "tools": list(catalogs[preset])}
        base = {"kind": "run", "case": case["id"], "preset": preset, "run": run,
                "model": clients[preset].model}
        async with gate:
            line = await _one(clients[preset], request, catalogs[preset], base, read_only)
        out.write(json.dumps(line, ensure_ascii=False) + "\n")
        out.flush()
        done += 1
        names = ",".join(c["name"] for c in line.get("calls", ())) or "text"
        sys.stderr.write(
            f"[{done}/{total}] {case['id']} {preset}#{run} -> {line['error'] or names} "
            f"({line.get('first_token_s') or '-'}s)\n",
        )

    async def cell(case: dict[str, Any], preset: str, out: TextIO) -> None:
        await job(case, preset, 0, out)  # writes the prompt cache runs 1.. read
        await asyncio.gather(*(job(case, preset, run, out) for run in range(1, args.runs)))

    with args.out.open("a", encoding="utf-8") as out:
        menu = menus[False]
        out.write(json.dumps({
            "kind": "menu", "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "tools": [t["name"] for t in menu], "chars": _menu_chars(menu),
            "native_chars": _menu_chars(menus[True]), "system_append": bool(extra),
        }) + "\n")
        await asyncio.gather(*(cell(case, preset, out) for case in cases for preset in clients))


def cmd_run(args: argparse.Namespace) -> int:
    """``run``: replay the cases against the presets and append the results."""
    root = Path(os.environ.get("JARVIS_RUNTIME_ROOT") or Path.home() / ".jarvis")
    load_env_file(root.expanduser())  # fill-only; the values are never read here
    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    registry = _exam_registry(config, args.cases.parent / "mcp-tools.json")
    menus = {native: _menu(registry, native=native) for native in (False, True)}
    menu = menus[False]
    read_only = {t.name: t.read_only for t in registry.for_caller(CallerPrincipal.JARVIS_LLM)}
    sys.stdout.write(
        f"menu: {len(menu)} tools, {_menu_chars(menu)} description+schema chars "
        f"({_menu_chars(menus[True])} with Anthropic's tool search)\n"
        f"{' '.join(t['name'] for t in menu)}\n",
    )
    sys.stdout.flush()
    asyncio.run(_run(args, menus, read_only if args.guards else None))
    return 0


def _history(request: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """The request's earlier tool calls (as :func:`_calls` shapes them) and tool outputs."""
    if "input" in request:  # Responses
        items = request["input"]
        calls = [one for one in items if one.get("type") == "function_call"]
        outputs = [one["output"] for one in items if one.get("type") == "function_call_output"]
        return _calls(calls), outputs
    messages = request["messages"]  # chat
    calls = [call["function"] for one in messages for call in one.get("tool_calls") or ()]
    return _calls(calls), [one["content"] for one in messages if one["role"] == "tool"]


def _trips(rule: Any, args: Any) -> bool:  # noqa: ANN401 - a substring list or an {arg: value} dict
    """Whether a call's arguments break one ``args_must_not`` rule."""
    if isinstance(rule, dict):
        return isinstance(args, dict) and all(
            k in args and (v == "*" or args[k] == v) for k, v in rule.items()
        )
    raw = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
    return any(s in raw for s in rule)


def load_patches(cases: Path) -> list[dict[str, str]]:
    """``prompt-patches.json`` beside ``cases``: ``[{old, new}]`` text the program now writes.

    A logged request carries the system prompt and history notes of its day; a patch replays it
    as the checkout would write it, so a tool rename and the lines naming it are examined
    together. [] if absent.
    """
    path = cases.parent / "prompt-patches.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def patched(request: Mapping[str, Any], patches: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    """``request`` with each patch applied wherever its text appears (prompt, history, results)."""
    raw = json.dumps(request, ensure_ascii=False)
    for one in patches:
        old, new = (json.dumps(one[k], ensure_ascii=False)[1:-1] for k in ("old", "new"))
        raw = raw.replace(old, new)
    out: dict[str, Any] = json.loads(raw)
    return out


def load_aliases(cases: Path) -> dict[str, str]:
    """``tool-aliases.json`` beside ``cases`` (old tool name -> its replacement); {} if absent."""
    path = cases.parent / "tool-aliases.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def usable_aliases(
    aliases: Mapping[str, str], menu: Collection[str] | None,
) -> dict[str, str]:
    """The aliases whose replacement was offered on ``menu``; all of them when none was recorded."""
    if menu is None:
        return dict(aliases)
    return {old: new for old, new in aliases.items() if new in menu}


def judge(  # noqa: C901 - one verdict from the expectation's rules.
    first: Mapping[str, Any], line: Mapping[str, Any], request: Mapping[str, Any],
    aliases: Mapping[str, str] | None = None,
) -> tuple[bool, str]:
    """Whether one run's first step satisfies ``expect.first``, and its short label.

    The tool names in the expectation, and in the history a repeat is looked for in, are read
    through ``aliases`` (see the module docstring).
    """
    named = (aliases or {}).get
    ok = {named(n, n) for n in first["ok"]}
    bad = {named(n, n) for n in first.get("bad", ())}
    rules: dict[str, list[Any]] = defaultdict(list)
    for tool_name, rule in (first.get("args_must_not") or {}).items():
        rules[named(tool_name, tool_name)].append(rule)
    musts = {named(n, n): one for n, one in (first.get("args_must") or {}).items()}
    if line.get("error"):
        return False, f"ERR({str(line['error']).split(':')[0]})"
    calls = line.get("calls") or []
    names = [c["name"] for c in calls] or ["text"]
    hit = set(names)
    why = []
    if hit & bad or not hit & ok:
        why.append(",".join(dict.fromkeys(names)))
    if len(calls) > first.get("max_calls", len(calls)):
        why.append(f"{len(calls)} calls")
    if names == ["text"] and any(re.search(rx, line.get("text") or "") for rx in first.get(
        "text_must_not", (),
    )):
        why.append("text~rule")
    # Misheard words (ASR): the call arguments or the answer must carry what he meant.
    said = (line.get("text") or "", json.dumps([c["args"] for c in calls], ensure_ascii=False))
    if first.get("must_mention") and not any(
        re.search(rx, part) for rx in first["must_mention"] for part in said
    ):
        why.append("missed")
    earlier, outputs = _history(request)
    earlier = [{**one, "name": named(one["name"], one["name"])} for one in earlier]
    for call in calls:
        name, args = call["name"], call["args"]
        cursor = args.get("cursor") if isinstance(args, dict) else None
        tripped = []
        if any(_trips(rule, args) for rule in rules.get(name, ())) or (
            name in musts and not any(_trips(one, args) for one in musts[name])
        ):
            tripped.append(f"{name}~args")
        if first.get("no_identical_repeat") and {"name": name, "args": args} in earlier:
            tripped.append(f"repeat:{name}")
        # Quoted, so a truncated copy of a real cursor is no match.
        if first.get("cursor_must_come_from_history") and cursor is not None and not any(
            json.dumps(cursor) in out for out in outputs
        ):
            tripped.append(f"cursor:{name}")
        why += [one for one in tripped if one not in why]
    label = "pass" if not why else f"FAIL({'+'.join(why)})"
    return (not why), label + ("(g)" if line.get("guarded") else "")


def _seen(line: Mapping[str, Any]) -> str:
    """A run's first-step tool names, for a case nobody scored."""
    return str(line["error"]).split(":")[0] if line.get("error") else (
        ",".join(c["name"] for c in line.get("calls") or ()) or "text"
    )


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def _cost(lines: Sequence[Mapping[str, Any]], table: Mapping[str, Any]) -> str:
    total, unpriced = 0.0, 0
    spent = [(line["model"], u) for line in lines
             for u in (line.get("usage"), (line.get("guarded") or {}).get("first_usage"))]
    for model, usage in spent:
        if not usage or usage["input"] is None or usage["output"] is None:
            continue
        one = compute_cost_usd(
            model, usage["input"], usage["output"], usage["cache_read"],
            usage["cache_write"], dict(table),
        )
        if one is None:
            unpriced += 1
        else:
            total += one
    return f"${total:.4f}" + (f" ({unpriced} unpriced)" if unpriced else "")


Runs = dict[tuple[str, str], list[dict[str, Any]]]
Verdicts = dict[tuple[str, str], list[tuple[bool, str]]]


def _cells(
    presets: Sequence[str], case_id: str, make: Callable[[str], str], have: Mapping[Any, Any],
) -> str:
    return " | ".join(make(p) for p in presets if (case_id, p) in have)


def _summary(
    presets: Sequence[str], runs: Runs, verdicts: Verdicts, table: Mapping[str, Any],
) -> list[str]:
    out = [f"{'preset':8s} {'passed':>8s} {'first-token med/p90 s':>22s}  cost", ""]
    for preset in presets:
        mine = [line for (_, p), ls in runs.items() if p == preset for line in ls]
        scored = [v for (_, p), v in verdicts.items() if p == preset]
        firsts = [line["first_token_s"] for line in mine if line.get("first_token_s")]
        passed = f"{sum(all(ok for ok, _ in v) for v in scored)}/{len(scored)}"
        timing = (f"{statistics.median(firsts):.2f} / {_percentile(firsts, _P90):.2f}"
                  if firsts else "n/a")
        guarded = sum(1 for line in mine if line.get("guarded"))
        out.append(f"{preset:8s} {passed:>8s} {timing:>22s}  "
                   f"{_cost(mine, table)} ({len(mine)} runs"
                   + (f", {guarded} guarded)" if guarded else ")"))
    return out


def _case_lines(
    cases: Mapping[str, Any], presets: Sequence[str], runs: Runs, verdicts: Verdicts,
) -> list[str]:
    out = ["", "scored cases:"]
    for case_id, case in cases.items():
        if any((case_id, p) in verdicts for p in presets):
            def mark(p: str, case_id: str = case_id) -> str:
                got = verdicts[case_id, p]
                return (f"{p}[{'PASS' if all(ok for ok, _ in got) else 'FAIL'}]: "
                        + " ".join(label for _, label in got))
            out.append(f"  {case_id}  {case['said'][:24]!r}  "
                       + _cells(presets, case_id, mark, verdicts))
    unscored = [c for c in cases.values()
                if "expect" not in c and any((c["id"], p) in runs for p in presets)]
    if unscored:
        out += ["", "unscored (run only, first-step tools):"]
        for case in unscored:
            def seen(p: str, case_id: str = case["id"]) -> str:
                return f"{p}: " + " ".join(_seen(line) for line in runs[case_id, p])
            out.append(f"  {case['id']}  {case['said'][:24]!r}  "
                       + _cells(presets, case["id"], seen, runs))
    return out


def cmd_score(args: argparse.Namespace) -> int:
    """``score``: the pass table, timings and cost of a results file."""
    cases = {c["id"]: c for c in json.loads(args.cases.read_text(encoding="utf-8"))}
    runs: Runs = defaultdict(list)
    menu: frozenset[str] | None = None  # the tools of the menu line the next runs followed
    for text in args.results.read_text(encoding="utf-8").splitlines():
        line = json.loads(text) if text.strip() else {}
        if line.get("kind") == "menu":
            menu = frozenset(line["tools"])
        elif line.get("kind") == "run" and line["case"] in cases:
            runs[line["case"], line["preset"]].append({**line, "_menu": menu})
    for lines in runs.values():
        lines.sort(key=lambda one: one["run"])
    presets = sorted({p for _, p in runs})
    aliases = load_aliases(args.cases)
    verdicts: Verdicts = {
        key: [
            judge(first, line, cases[key[0]]["request"], usable_aliases(aliases, line["_menu"]))
            for line in lines
        ]
        for key, lines in runs.items()
        if (first := (cases[key[0]].get("expect") or {}).get("first"))
    }
    out = _summary(presets, runs, verdicts, load_pricing_table(REPO / "data" / "pricing.json"))
    out += _case_lines(cases, presets, runs, verdicts)
    errors = [line for lines in runs.values() for line in lines if line.get("error")]
    if errors:
        out += ["", "errors (count as failures):"]
        out += [f"  {e['case']} {e['preset']}#{e['run']}: {e['error']}" for e in errors]
    sys.stdout.write("\n".join(out) + "\n")
    return 0


def main() -> int:
    """Parse arguments and run the subcommand."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--cases", type=Path, required=True)
    run.add_argument("--presets", type=lambda s: s.split(","), required=True)
    run.add_argument("--runs", type=int, default=1)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--only", type=lambda s: s.split(","))
    run.add_argument("--config", type=Path, default=REPO / "config" / "jarvis.yaml")
    run.add_argument("--system-append", type=Path)
    run.add_argument("--concurrency", type=int, default=6)
    run.add_argument("--guards", action="store_true",
                     help="apply the loop's repeat and unbacked-claim guards, then one more step")
    run.set_defaults(func=cmd_run)
    score = sub.add_parser("score")
    score.add_argument("results", type=Path)
    score.add_argument("--cases", type=Path, required=True)
    score.set_defaults(func=cmd_score)
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
