"""Tests for ``core.cc_jsonl_reader``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import cc_jsonl_reader as ccr


# ---------------------------------------------------------------------------
# encode_cwd_for_cc
# ---------------------------------------------------------------------------


def test_encode_cwd_basic():
    assert ccr.encode_cwd_for_cc("/Users/foo/bar") == "-Users-foo-bar"


def test_encode_cwd_root():
    assert ccr.encode_cwd_for_cc("/") == "-"


# ---------------------------------------------------------------------------
# find_active_jsonl
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_cc_root(tmp_path, monkeypatch):
    """Point CC_PROJECTS_ROOT at an isolated tmp dir."""
    root = tmp_path / "claude_projects"
    root.mkdir()
    monkeypatch.setattr(ccr, "CC_PROJECTS_ROOT", root)
    return root


def _write_jsonl(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")


def test_find_returns_none_when_dir_missing(fake_cc_root):
    assert ccr.find_active_jsonl("/no/such/cwd") is None


def test_find_returns_none_when_no_jsonl(fake_cc_root):
    project_dir = fake_cc_root / "-tmp-empty"
    project_dir.mkdir()
    assert ccr.find_active_jsonl("/tmp/empty") is None


def test_find_picks_most_recent_mtime(fake_cc_root):
    project_dir = fake_cc_root / "-tmp-proj"
    project_dir.mkdir()
    older = project_dir / "older.jsonl"
    newer = project_dir / "newer.jsonl"
    older.write_text("{}\n", encoding="utf-8")
    newer.write_text("{}\n", encoding="utf-8")
    import os
    os.utime(older, (1000, 1000))
    os.utime(newer, (2000, 2000))

    result = ccr.find_active_jsonl("/tmp/proj")
    assert result == newer


def test_find_skips_subagent_dirs(fake_cc_root):
    """Subagent jsonls live under <session-uuid>/subagents/ — must be ignored."""
    project_dir = fake_cc_root / "-tmp-proj"
    project_dir.mkdir()
    main = project_dir / "main.jsonl"
    main.write_text("{}\n", encoding="utf-8")

    sub_dir = project_dir / "abc-123" / "subagents"
    sub_dir.mkdir(parents=True)
    sub = sub_dir / "agent-xyz.jsonl"
    sub.write_text("{}\n", encoding="utf-8")
    import os
    # Make subagent newer to ensure it would win on mtime alone
    os.utime(main, (1000, 1000))
    os.utime(sub, (5000, 5000))

    result = ccr.find_active_jsonl("/tmp/proj")
    assert result == main


# ---------------------------------------------------------------------------
# read_last_assistant
# ---------------------------------------------------------------------------


def _assistant_event(blocks: list[dict]) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": blocks},
    }


def _user_event(text: str = "hi") -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def test_read_returns_empty_when_no_assistant(tmp_path):
    p = tmp_path / "x.jsonl"
    _write_jsonl(p, [_user_event("only user message")])
    turn = ccr.read_last_assistant(p)
    assert turn.empty is True
    assert turn.text == ""
    assert turn.tool_calls == []


def test_read_extracts_text_blocks(tmp_path):
    p = tmp_path / "x.jsonl"
    _write_jsonl(
        p,
        [
            _user_event("hi"),
            _assistant_event(
                [
                    {"type": "text", "text": "Hello"},
                    {"type": "text", "text": "World"},
                ]
            ),
        ],
    )
    turn = ccr.read_last_assistant(p)
    assert turn.empty is False
    assert turn.text == "Hello\n\nWorld"
    assert turn.tool_calls == []


def test_read_extracts_tool_calls(tmp_path):
    p = tmp_path / "x.jsonl"
    _write_jsonl(
        p,
        [
            _assistant_event(
                [
                    {"type": "text", "text": "Let me check"},
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "ls -la /tmp", "description": "list"},
                    },
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "/etc/hosts"},
                    },
                ]
            ),
        ],
    )
    turn = ccr.read_last_assistant(p)
    assert turn.text == "Let me check"
    assert len(turn.tool_calls) == 2
    assert turn.tool_calls[0].name == "Bash"
    assert turn.tool_calls[0].input_summary == "ls -la /tmp"
    assert turn.tool_calls[1].name == "Read"
    assert turn.tool_calls[1].input_summary == "/etc/hosts"


def test_read_picks_last_assistant_only(tmp_path):
    """Two assistant turns — read should return the SECOND one only."""
    p = tmp_path / "x.jsonl"
    _write_jsonl(
        p,
        [
            _assistant_event([{"type": "text", "text": "first reply"}]),
            _user_event("follow up"),
            _assistant_event([{"type": "text", "text": "second reply"}]),
        ],
    )
    turn = ccr.read_last_assistant(p)
    assert turn.text == "second reply"


def test_read_skips_malformed_lines(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text(
        "not json\n"
        + json.dumps(_assistant_event([{"type": "text", "text": "ok"}]))
        + "\n"
        + "{broken\n",
        encoding="utf-8",
    )
    turn = ccr.read_last_assistant(p)
    assert turn.text == "ok"


def test_read_returns_empty_when_file_missing(tmp_path):
    turn = ccr.read_last_assistant(tmp_path / "does-not-exist.jsonl")
    assert turn.empty is True


def test_read_handles_grep_glob_webfetch_task(tmp_path):
    """Tool input summary picks the right field per tool name."""
    p = tmp_path / "x.jsonl"
    _write_jsonl(
        p,
        [
            _assistant_event(
                [
                    {"type": "tool_use", "name": "Grep", "input": {"pattern": "TODO"}},
                    {"type": "tool_use", "name": "Glob", "input": {"pattern": "**/*.py"}},
                    {
                        "type": "tool_use",
                        "name": "WebFetch",
                        "input": {"url": "https://example.com"},
                    },
                    {
                        "type": "tool_use",
                        "name": "Task",
                        "input": {"description": "investigate bug"},
                    },
                ]
            ),
        ],
    )
    turn = ccr.read_last_assistant(p)
    summaries = [tc.input_summary for tc in turn.tool_calls]
    assert summaries == ["TODO", "**/*.py", "https://example.com", "investigate bug"]


def test_truncate_long_input(tmp_path):
    p = tmp_path / "x.jsonl"
    long_cmd = "echo " + "x" * 500
    _write_jsonl(
        p,
        [_assistant_event([{"type": "tool_use", "name": "Bash", "input": {"command": long_cmd}}])],
    )
    turn = ccr.read_last_assistant(p)
    assert len(turn.tool_calls[0].input_summary) == 120
    assert turn.tool_calls[0].input_summary.endswith("...")


# ---------------------------------------------------------------------------
# find_recent_jsonls
# ---------------------------------------------------------------------------


def test_find_recent_jsonls_empty_when_dir_missing(fake_cc_root):
    assert ccr.find_recent_jsonls("/no/such/cwd", n=3) == []


def test_find_recent_jsonls_returns_top_n_sorted_newest_first(fake_cc_root):
    project_dir = fake_cc_root / "-tmp-proj"
    project_dir.mkdir()
    files = []
    import os
    for i in range(5):
        f = project_dir / f"sess-{i}.jsonl"
        f.write_text("{}\n", encoding="utf-8")
        os.utime(f, (1000 + i, 1000 + i))  # i=4 newest, i=0 oldest
        files.append(f)

    result = ccr.find_recent_jsonls("/tmp/proj", n=3)
    assert result == [files[4], files[3], files[2]]


def test_find_recent_jsonls_caps_at_n(fake_cc_root):
    project_dir = fake_cc_root / "-tmp-proj"
    project_dir.mkdir()
    for i in range(5):
        (project_dir / f"sess-{i}.jsonl").write_text("{}\n", encoding="utf-8")
    assert len(ccr.find_recent_jsonls("/tmp/proj", n=3)) == 3
    assert len(ccr.find_recent_jsonls("/tmp/proj", n=10)) == 5


def test_find_recent_jsonls_skips_subagent_dirs(fake_cc_root):
    project_dir = fake_cc_root / "-tmp-proj"
    project_dir.mkdir()
    (project_dir / "main.jsonl").write_text("{}\n", encoding="utf-8")
    sub_dir = project_dir / "abc-uuid" / "subagents"
    sub_dir.mkdir(parents=True)
    (sub_dir / "agent-x.jsonl").write_text("{}\n", encoding="utf-8")

    result = ccr.find_recent_jsonls("/tmp/proj", n=5)
    paths = {p.name for p in result}
    assert "main.jsonl" in paths
    assert "agent-x.jsonl" not in paths


# ---------------------------------------------------------------------------
# read_recent_exchange
# ---------------------------------------------------------------------------


def _user_text_event(text: str) -> dict:
    """Real user prompt (text content blocks)."""
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _user_tool_result_event() -> dict:
    """cc-emitted user-typed event for a tool result (NOT a real prompt)."""
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"tool_use_id": "abc", "type": "tool_result", "content": "result"}
            ],
        },
    }


def test_read_recent_exchange_extracts_user_and_assistant(tmp_path):
    p = tmp_path / "x.jsonl"
    _write_jsonl(
        p,
        [
            _user_text_event("帮我看下 router 的 bug"),
            _assistant_event(
                [
                    {"type": "text", "text": "Let me grep for that"},
                    {"type": "tool_use", "name": "Grep", "input": {"pattern": "router"}},
                ]
            ),
        ],
    )
    cand = ccr.read_recent_exchange(p)
    assert cand.empty is False
    assert cand.last_user_prompt == "帮我看下 router 的 bug"
    assert cand.last_assistant_text == "Let me grep for that"
    assert len(cand.recent_tool_calls) == 1
    assert cand.recent_tool_calls[0].name == "Grep"
    assert cand.session_id == "x"


def test_read_recent_exchange_skips_tool_result_messages(tmp_path):
    """Real user prompts must be distinguished from cc's tool_result feedback."""
    p = tmp_path / "x.jsonl"
    _write_jsonl(
        p,
        [
            _user_text_event("real user prompt here"),
            _assistant_event(
                [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]
            ),
            _user_tool_result_event(),  # MUST be skipped
            _assistant_event([{"type": "text", "text": "done"}]),
        ],
    )
    cand = ccr.read_recent_exchange(p)
    assert cand.last_user_prompt == "real user prompt here"
    assert cand.last_assistant_text == "done"


def test_read_recent_exchange_truncates_long_user_prompt(tmp_path):
    p = tmp_path / "x.jsonl"
    long_prompt = "P" * 500
    _write_jsonl(
        p,
        [
            _user_text_event(long_prompt),
            _assistant_event([{"type": "text", "text": "ok"}]),
        ],
    )
    cand = ccr.read_recent_exchange(p)
    assert len(cand.last_user_prompt) == ccr._CANDIDATE_USER_PROMPT_CHARS
    assert cand.last_user_prompt.endswith("...")


def test_read_recent_exchange_truncates_long_assistant_text(tmp_path):
    p = tmp_path / "x.jsonl"
    _write_jsonl(
        p,
        [
            _user_text_event("hi"),
            _assistant_event([{"type": "text", "text": "A" * 1000}]),
        ],
    )
    cand = ccr.read_recent_exchange(p)
    assert len(cand.last_assistant_text) == ccr._CANDIDATE_ASSISTANT_CHARS
    assert cand.last_assistant_text.endswith("...")


def test_read_recent_exchange_caps_tool_calls_at_5(tmp_path):
    p = tmp_path / "x.jsonl"
    blocks = [
        {"type": "tool_use", "name": "Bash", "input": {"command": f"echo {i}"}}
        for i in range(8)
    ]
    _write_jsonl(p, [_user_text_event("run stuff"), _assistant_event(blocks)])
    cand = ccr.read_recent_exchange(p)
    assert len(cand.recent_tool_calls) == 5


def test_read_recent_exchange_empty_when_no_assistant(tmp_path):
    p = tmp_path / "x.jsonl"
    _write_jsonl(p, [_user_text_event("hi")])
    cand = ccr.read_recent_exchange(p)
    assert cand.empty is True
    assert cand.session_id == "x"


def test_read_recent_exchange_session_id_from_filename(tmp_path):
    p = tmp_path / "abc-uuid-1234.jsonl"
    _write_jsonl(p, [_assistant_event([{"type": "text", "text": "hi"}])])
    cand = ccr.read_recent_exchange(p)
    assert cand.session_id == "abc-uuid-1234"


def test_read_recent_exchange_picks_latest_assistant_when_multiple(tmp_path):
    """Reverse scan: should return the LAST assistant, not earlier ones."""
    p = tmp_path / "x.jsonl"
    _write_jsonl(
        p,
        [
            _user_text_event("first"),
            _assistant_event([{"type": "text", "text": "first reply"}]),
            _user_text_event("second"),
            _assistant_event([{"type": "text", "text": "second reply"}]),
        ],
    )
    cand = ccr.read_recent_exchange(p)
    assert cand.last_assistant_text == "second reply"
    assert cand.last_user_prompt == "second"
