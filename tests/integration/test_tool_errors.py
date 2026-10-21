"""ADR 0247: a tool's error says what happened and the next step, and names the bad field.

Each row is a call that fails and the words the model must be able to act on. The calls run the
real handlers; nothing here reaches the network, the screen or a model.
"""

from __future__ import annotations

import json
import sqlite3
import struct
import subprocess
import zlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from jarvis.execution import tools
from jarvis.execution.highlight_tool import build_highlight_tool
from jarvis.execution.location_tool import build_where_tool
from jarvis.execution.mail_draft_tool import build_mail_draft_tool
from jarvis.execution.night_tools import build_night_tools
from jarvis.execution.picture_tool import build_picture_tool
from jarvis.execution.place_tools import build_place_tools
from jarvis.execution.tool_search import build_tool_search
from jarvis.execution.tools import (
    CallerNotAllowedError,
    Tool,
    ToolContext,
    ToolError,
    UnknownToolError,
    build_default_registry,
)
from jarvis.execution.voice_tools import build_voice_tool
from jarvis.execution.weather_tool import build_weather_tool
from jarvis.runtime.dashboard import MailDrafts, ViewState
from jarvis.shared import CallerPrincipal
from tests.support.tool_world import ToolWorld, tool_chain, tool_request

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from jarvis.execution.tools import ToolDefinition

_ASK = {"question": "Where to?", "fields": [{"label": "address"}]}


def _boom(*_args: object, **_kwargs: object) -> dict[str, Any]:
    msg = "no signal"
    raise RuntimeError(msg)


class _Stub:
    """A stand-in for the runtime object a tool is handed: any attribute is a harmless call."""

    def __getattr__(self, _name: str) -> Callable[..., dict[str, Any]]:
        return lambda *_a, **_k: {}


def _flat(tmp_path: Path) -> dict[str, Tool]:
    """Every flat tool this module drives, built over stand-ins, by name."""
    drafts = MailDrafts(ViewState())
    built = [
        *build_default_registry(
            obsidian_vault_root=tmp_path, web_search_provider="ddgs",
            memory_db_path=tmp_path / "memory.db", vision_client=cast("Any", _Stub()),
        ).get_definitions(),
        *build_weather_tool(_boom),
        *build_where_tool(_boom),
        *build_place_tools(tmp_path / "memory.db"),
        *build_highlight_tool(lambda *_: {}),
        *build_voice_tool(cast("Any", _Stub())),
        *build_picture_tool(cast("Any", _Stub()), None),
        *build_mail_draft_tool(drafts),
        *build_night_tools(cast("Any", _Stub())),
    ]
    return {t.name: t for t in built if isinstance(t, Tool)}


def _error(tool: Tool, args: dict[str, Any], ctx: Any = None) -> ToolError:  # noqa: ANN401
    with pytest.raises(ToolError) as caught:
        tool.handler(args, cast("ToolContext", ctx))
    return caught.value


# (tool, arguments, code, every phrase the message must hold)
FLAT_ROWS: tuple[tuple[str, dict[str, Any], str, tuple[str, ...]], ...] = (
    ("ask_user", {**_ASK, "question": ""}, "invalid_argument",
     ("ask_user: question must be 1-200 characters", "make it one short sentence")),
    ("ask_user", {**_ASK, "fields": []}, "invalid_argument",
     ("fields must list 1-4 details", "ask for the rest in words")),
    ("ask_user", {**_ASK, "fields": ["address"]}, "invalid_argument",
     ('each entry in fields must be an object, e.g. {"label": "delivery address"}',)),
    ("ask_user", {**_ASK, "fields": [{"label": "a", "choices": ["x"]}]}, "invalid_argument",
     ("choices must list 2-6 options", "leave choices out for a typed answer")),
    ("ask_user", {"question": "Your password?", "fields": [{"label": "pw"}]}, "secret_field",
     ("never asks for passwords", "tell him to enter those on the site itself")),
    ("open_path", {}, "invalid_argument",
     ("open_path: query must be the spoken name of the file or folder to open (got None)",)),
    ("open_path", {"query": "x", "target_kind": "dir"}, "invalid_argument",
     ("target_kind must be one of: file, folder, any (got 'dir')",)),
    ("open_path", {"query": "x", "app": "emacs"}, "invalid_argument",
     ("app must be one of: default, vscode (got 'emacs')",)),
    ("web_search", {}, "invalid_argument", ("query must be the words to search for",)),
    ("web_fetch", {}, "invalid_argument", ("url must be the page address, http or https",)),
    ("web_fetch", {"url": "ftp://example.com/x"}, "ssrf_guard_refused",
     ("was refused (scheme 'ftp' is not in the http/https allowlist)",
      "only public http(s) pages can be fetched")),
    ("weather", {}, "network_error",
     ("weather: the forecast could not be read (RuntimeError)", "use web_search")),
    ("where_am_i", {}, "location_unavailable",
     ("this Mac's location could not be read (no signal)", "ask the user where he is")),
    ("save_place", {}, "invalid_argument",
     ("save_place: name (what he calls the place) and where", "are both required")),
    ("save_place", {"name": "gym", "where": "here", "radius_m": "wide"}, "invalid_argument",
     ("save_place: radius_m must be a whole number of metres (got 'wide')",)),
    ("forget_place", {}, "invalid_argument",
     ("forget_place: place is required", "pass a kept place's id or name from places")),
    ("forget_place", {"place": "gym"}, "not_found",
     ("forget_place: no kept place 'gym'", "call places to see what is kept")),
    ("mark_highlight", {}, "invalid_argument",
     ('mark_highlight: day (YYYY-MM-DD, "today" or "yesterday") and time (HH:MM) are required',)),
    ("mark_highlight", {"day": "today", "time": "9", "label": "x" * 500}, "invalid_argument",
     ("mark_highlight: label is over", "shorten it")),
    ("set_voice", {"volume": "loud"}, "invalid_argument",
     ("set_voice: volume must be one of: louder_a_bit, louder_a_lot, quieter_a_bit, quieter_a_lot",
      "(got 'loud')")),
    ("look_at_picture", {}, "bad_arguments",
     ('look_at_picture: picture (the id in its "[Picture note ... id XXXXXXXX]" line, '
      'or "latest") and question are both required',)),
    ("write_mail_draft", {"letter_id": "m1", "text": "Hi"}, "invalid_argument",
     ("write_mail_draft: that letter is not open on the Dashboard, so no draft can go under it",
      "give him the reply in words")),
    ("start_night_run", {"hours": 99}, "invalid_argument",
     ("start_night_run: hours must be between 0.25 and 12", "or left out for the default length")),
    ("start_night_run", {"until": "tonight"}, "invalid_argument",
     ("start_night_run: until must be a local time HH:MM, e.g. 07:30, or left out",)),
)


@pytest.mark.parametrize("row", FLAT_ROWS)
def test_a_failing_call_says_what_happened_and_what_to_do_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    row: tuple[str, dict[str, Any], str, tuple[str, ...]],
) -> None:
    """The message holds the phrases, its code is the one the dispatcher records, no JSON in it."""
    name, args, code, phrases = row
    monkeypatch.setattr(tools, "_ddgs_search_backend", _boom)
    ctx = SimpleNamespace(conn=sqlite3.connect(":memory:"), action_id="A1")
    error = _error(_flat(tmp_path)[name], args, ctx)
    assert error.code == code
    for phrase in phrases:
        assert phrase in str(error), (name, phrase, str(error))
    assert not str(error).lstrip().startswith(("{", "["))  # a sentence, not an encoded object


def test_a_search_that_fails_names_the_provider_and_the_next_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The backend's own words ride along; the model is told to say it failed or fetch a page."""
    monkeypatch.setattr(tools, "_ddgs_search_backend", _boom)
    error = _error(_flat(tmp_path)["web_search"], {"query": "oat milk"})
    assert error.code == "web_search_backend_error"
    assert str(error) == (
        "web_search: the ddgs search did not answer (no signal); tell him the search "
        "failed, or try web_fetch on a page he names"
    )


def test_the_clipboard_that_cannot_be_read_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The clipboard tool failing to run, and exiting non-zero, are one sentence each."""
    def missing(*_a: object, **_k: object) -> None:
        msg = "pbpaste gone"
        raise OSError(msg)

    monkeypatch.setattr(subprocess, "run", missing)
    clip = _flat(tmp_path)["read_clipboard"]
    assert str(_error(clip, {})) == (
        "read_clipboard: the clipboard could not be read (pbpaste gone); tell him it failed"
    )
    monkeypatch.setattr(
        subprocess, "run", lambda *_a, **_k: subprocess.CompletedProcess([], 1, b"", b"denied"),
    )
    assert str(_error(clip, {})) == (
        "read_clipboard: the clipboard could not be read (exit 1: denied); tell him it failed"
    )


# ToolDefinition tools answer through the dispatcher's event rows.
DEFINITION_ROWS: tuple[tuple[str, dict[str, Any], str, tuple[str, ...]], ...] = (
    ("search_notes", {}, "invalid_argument",
     ("search_notes: query must be the words to look for in his notes (got None)",)),
    ("read_file", {"target": "x"}, "no_resolved_target",
     ("read_file: no file was resolved from that name; ask him which file he means",)),
    ("open_url", {}, "invalid_argument",
     ("open_url: url must be the page address, http or https",)),
    ("open_url", {"url": "ftp://example.com/x"}, "ssrf_guard_refused",
     ("open_url: 'ftp://example.com/x' was refused (scheme 'ftp' is not in the http/https",
      "only public http(s) pages can be opened")),
)


@pytest.mark.parametrize("row", DEFINITION_ROWS)
def test_a_failing_definition_tool_answers_in_the_same_words(
    tmp_path: Path, row: tuple[str, dict[str, Any], str, tuple[str, ...]],
) -> None:
    """The dispatcher's recorded output is one JSON object whose error is a sentence."""
    name, args, code, phrases = row
    registry = build_default_registry(obsidian_vault_root=tmp_path / "vault")
    held = [cast("ToolDefinition", t) for t in registry.get_definitions() if t.name == name]
    fx = ToolWorld(tmp_path / "run", tools=tuple(held))
    try:
        fx.dispatch(tool_request(name, "E1", arguments=args))
        output = json.loads(tool_chain(fx, "E1")["tool_output"])
    finally:
        fx.close()
    assert output["code"] == code
    for phrase in phrases:
        assert phrase in output["error"], (name, phrase, output["error"])
    assert not output["error"].lstrip().startswith(("{", "["))


def test_tool_search_errors_name_the_argument_and_what_to_pass() -> None:
    """An empty query and a limit that is not a whole number are told how to be right."""
    (search,) = build_tool_search(
        [Tool(
            name="late_note", description="d", input_schema={"type": "object"},
            handler=lambda _a, _c: {}, allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L0", read_only=True, deferred=True,
        )],
        {},
    )
    assert str(_error(search, {"query": " "})) == (
        'tool_search: query is empty; pass English keywords for the tool you need, e.g. "plugin"'
    )
    for limit in (-2, "many"):
        assert str(_error(search, {"query": "note", "limit": limit})) == (
            "tool_search: limit must be a whole number from 1 (default 8)"
        )


def test_the_registry_says_which_tool_is_missing_and_who_may_call(tmp_path: Path) -> None:
    """An unknown name points at tool_search; a refused caller lists who is allowed."""
    fx = ToolWorld(tmp_path, tools=(_ask_user(),))
    try:
        with pytest.raises(UnknownToolError) as unknown:
            fx.dispatch(tool_request("nowhere", "U1"))
        assert str(unknown.value) == (
            "unknown tool: 'nowhere'; call only a tool in your list, or find one with tool_search"
        )
        with pytest.raises(CallerNotAllowedError) as refused:
            fx.dispatch(tool_request("ask_user", "U2", caller=CallerPrincipal.OBSERVER))
        assert str(refused.value) == (
            "tool 'ask_user' is not callable from caller_principal='observer'; "
            "allowed: jarvis_llm"
        )
    finally:
        fx.close()


def _ask_user() -> Tool:
    found = next(t for t in build_default_registry().get_definitions() if t.name == "ask_user")
    return cast("Tool", found)


def test_a_tool_that_crashes_is_reported_as_failed_not_as_a_repeatable_error(
    tmp_path: Path,
) -> None:
    """An exception the tool did not word becomes one sentence that says not to repeat the call."""
    crash = Tool(
        name="crash", description="d", input_schema={"type": "object"},
        handler=lambda _a, _c: _boom(), allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
        risk_level="L0", read_only=True,
    )
    fx = ToolWorld(tmp_path, tools=(crash,))
    try:
        fx.dispatch(tool_request("crash", "C1"))
        output = json.loads(tool_chain(fx, "C1")["tool_output"])
    finally:
        fx.close()
    assert output == {
        "error": "crash failed unexpectedly (RuntimeError: no signal); tell him it failed, "
        "and do not repeat the same call",
        "code": "unexpected_error",
    }


def _png(width: int, height: int, row: Callable[[int], bytes]) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(
            ">I", zlib.crc32(kind + body) & 0xFFFFFFFF,
        )

    raw = b"".join(b"\x00" + row(y) for y in range(height))
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    )


class _Vision:
    def describe_image(self, _path: Path, *, question: str | None = None, **_k: object) -> str:
        return f"a window ({question})"


@pytest.mark.parametrize(
    ("label", "image", "blank"),
    [
        ("a display that is asleep captures black", _png(1568, 980, lambda _y: bytes(4 * 1568)),
         True),
        ("a screen with windows on it", _png(
            400, 300, lambda y: bytes((x * y + x) % 251 for x in range(4 * 400)),
        ), False),
    ],
)
def test_screen_look_says_plainly_when_the_display_is_asleep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str, image: bytes, *, blank: bool,
) -> None:
    """A one-colour capture is not described: no vision call, a sentence that says why."""
    def capture(path: Path, **_k: object) -> subprocess.CompletedProcess[bytes]:
        path.write_bytes(image)
        return subprocess.CompletedProcess([], 0, b"", b"")

    monkeypatch.setattr(tools, "_run_screencapture", capture)
    monkeypatch.setattr(
        tools, "_run_sips_downscale",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, b"", b""),
    )
    look = tools._make_screen_look(  # noqa: SLF001
        vision_client=cast("Any", _Vision()), max_width_px=1568,
    )
    ctx = SimpleNamespace(runtime_paths=SimpleNamespace(artifacts_root=tmp_path), action_id="S1")
    if not blank:
        done = look.handler({"question": "what is open?"}, cast("ToolContext", ctx))
        assert done["description"] == "a window (what is open?)", label
        return
    error = _error(look, {"question": "what is open?"}, ctx)
    assert error.code == "screen_blank", label
    assert str(error) == (
        "screen_look: the screenshot is one flat colour, so the display is probably asleep or "
        "locked; tell him you cannot see the screen until it is awake"
    )
