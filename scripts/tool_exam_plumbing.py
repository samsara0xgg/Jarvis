"""Tool exam, plumbing layer (ADR 0317): scripted model replies through the real ``decide()`` loop.

No model is called and the network is blocked. An item is JSON, like a tool-layer item (see
``scripts/tool_exam_tools.py`` for ``id``, ``at``, ``zone``, ``state``, ``setup``, ``recording``,
``expect.events_after`` and ``expect.state_after``, which mean the same here) with ``"layer":
"plumbing"`` and, in place of ``tool`` and ``args``:

- ``turns``: ``[{said, script, after_s?}]``; a single turn may give ``said`` and ``script`` at the
  top. ``script`` is the model's replies in order, each ``{text?, tool_calls?: [{name, arguments}
  or {name, arguments_json}]}`` (``arguments_json`` is sent raw, so it can be malformed).
  ``after_s`` lets that many seconds pass before the turn, on the frozen clock.
- ``expect.text_must`` / ``text_must_not``: regexes on the last turn's answer.
- ``expect.model_requests``: how many requests the model got, per turn (one number for one turn).
- ``expect.request_has``: ``[{turn?, request, role?, tool?, matches?, not_matches?}]``; some message
  of that request (0-based; ``tool`` names the call a tool message answers) matches, or does not.

The loop runs on the booted sandbox runtime, so its registry, gate, guards and event log are the
program's own. Tier 0 is off: this layer judges what the program does with a model's reply.
"""

from __future__ import annotations

import re
import signal
import uuid
from typing import TYPE_CHECKING, Any, cast

from jarvis.decision import DecideContext, decide
from jarvis.runtime import _confirmation_ttl_ms, _max_tool_iterations
from jarvis.state.event_log import emit_event
from scripts.tool_exam_steps import ScriptedModel
from scripts.tool_exam_tools import (
    ITEM_TIMEOUT_S,
    Item,
    Recording,
    _check_events,
    _events_since,
    _Transport,
    dispatch,
    sandbox,
    state_failures,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from jarvis.decision.llm import LLMClient
    from scripts.tool_exam_tools import Box


def _turns(raw: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return list(raw.get("turns") or [{"said": raw["said"], "script": raw["script"]}])


def play_turn(box: Box, said: str, model: ScriptedModel) -> str:
    """One typed utterance through ``decide()`` with ``model`` answering; the answer text."""
    runtime = box.runtime
    turn_id = f"T{uuid.uuid4().hex[:12]}"
    trigger = emit_event(
        runtime.conn,
        type="surface.user_intent",
        payload={"transcript": said, "turn_id": turn_id, "channel": "cli_stdin"},
        correlation={"turn_id": turn_id},
    )
    ctx = DecideContext(
        conn=runtime.conn,
        runtime_paths=runtime.runtime_paths,
        tool_registry=cast("Any", runtime.tool_registry),
        lifecycle=cast("Any", runtime.lifecycle),
        llm_client=cast("LLMClient", model),
        system_prompt=runtime.system_prompt,
        tier0_table=(),
        confirmation_ttl_ms=_confirmation_ttl_ms(runtime.config),
        confirm_grammar_table=runtime.confirm_grammar_table,
        max_tool_iterations=_max_tool_iterations(runtime.config, trigger),
        wave1_features=runtime.wave1_features,
        reply_language=str(runtime.config.get("reply_language", "follow")),
    )
    plan = decide(trigger, ctx).response_plan
    return plan.text if plan is not None else ""


def _messages(request: Mapping[str, Any], role: str | None, tool: str | None) -> list[str]:
    """The contents of the request's messages of ``role`` (and, for a tool message, of ``tool``)."""
    names = {
        c["id"]: c["function"]["name"]
        for m in request["messages"]
        for c in m.get("tool_calls") or ()
    }
    return [
        str(m.get("content") or "")
        for m in request["messages"]
        if (role is None or m["role"] == role)
        and (tool is None or names.get(m.get("tool_call_id")) == tool)
    ]


def _judge(
    box: Box,
    expect: Mapping[str, Any],
    texts: Sequence[str],
    models: Sequence[ScriptedModel],
    after: int,
) -> list[str]:
    failures = []
    last = texts[-1] if texts else ""
    failures += [
        f"answer lacks /{p}/: {last[:120]!r}"
        for p in expect.get("text_must", ())
        if not re.search(p, last)
    ]
    failures += [
        f"answer has /{p}/: {last[:120]!r}"
        for p in expect.get("text_must_not", ())
        if re.search(p, last)
    ]
    if "model_requests" in expect:
        want = expect["model_requests"]
        want = want if isinstance(want, list) else [want]
        got = [len(m.requests) for m in models]
        if got != want:
            failures.append(f"model requests per turn: expected {want}, got {got}")
    for check in expect.get("request_has", ()):
        turn, index = check.get("turn", 0), check["request"]
        if turn >= len(models) or index >= len(models[turn].requests):
            failures.append(f"turn {turn} request {index}: the model was not asked that many times")
            continue
        contents = _messages(models[turn].requests[index], check.get("role"), check.get("tool"))

        def hit(p: str, contents: list[str] = contents) -> bool:
            return any(re.search(p, c, re.DOTALL) for c in contents)

        where = f"turn {turn} request {index} {check.get('role') or ''} {check.get('tool') or ''}"
        if "matches" in check and not hit(check["matches"]):
            failures.append(f"{where.strip()}: nothing matches /{check['matches']}/")
        if "not_matches" in check and hit(check["not_matches"]):
            failures.append(f"{where.strip()}: something matches /{check['not_matches']}/")
    failures += _check_events(expect.get("events_after") or {}, _events_since(box, after))
    return failures + state_failures(box, expect.get("state_after", ()))


def run_item(item: Item) -> list[str]:
    """Run one plumbing item; the list of what failed (empty = passed)."""
    raw = item.raw
    rec_path = raw.get("recording")
    recording = Recording.load(item.path.parent / rec_path) if rec_path else Recording()
    transport = _Transport(recording, live=False)
    failures: list[str] = []

    def alarm(*_: object) -> None:
        msg = f"timed out after {ITEM_TIMEOUT_S}s"
        raise TimeoutError(msg)

    previous = signal.signal(signal.SIGALRM, alarm)
    signal.alarm(ITEM_TIMEOUT_S)
    try:
        with transport, sandbox(item, recording) as box:
            for step in raw.get("setup") or ():
                dispatch(box, step["tool"], step.get("args") or {}, raw.get("said", ""))
            after = int(
                box.runtime.conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
            )
            texts, models = [], []
            for turn in _turns(raw):
                if turn.get("after_s"):
                    box.clock.shift(turn["after_s"])
                model = ScriptedModel(turn["script"])
                models.append(model)
                texts.append(play_turn(box, turn["said"], model))
            failures += _judge(box, raw.get("expect") or {}, texts, models, after)
    except (LookupError, RuntimeError, TimeoutError) as exc:
        failures.append(f"{type(exc).__name__}: {exc}")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    return failures + [f"provider call missing from the recording: {m}" for m in transport.misses]
