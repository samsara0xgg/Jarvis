"""Unit tests for :mod:`jarvis.execution.codex_action` (ADR-0002 Step 7).

All tests are LLM-free and Codex-free. The real
:class:`jarvis.execution.codex_client.CodexAppServerClient` is monkeypatched
out with :class:`FakeClient`, which accepts the same constructor kwargs
(``codex_bin``, ``extra_args``, ``env``) and surfaces a pre-canned
notification stream. The ``subprocess`` call used by
:func:`ensure_codex_version_supported` is also patched.

Coverage:

* TOML escape helpers (``_toml_str`` / ``_toml_list_quote``) against
  weird-path fixtures: spaces, single/double quotes, backslashes,
  non-ASCII.
* Driver happy path: all 8 ``-c`` flags appear in ``extra_args``;
  ``submit_report`` capture surfaces the structured args; ``turn_id`` /
  tokens flow through.
* Multi-call capture: when the queue carries multiple submit_report tool
  calls, every one ends up in ``submit_report_calls`` and the first is
  surfaced via ``submit_report``.
* Failure paths: ``initialize`` failure, ``thread/start`` failure,
  ``turn/start`` failure, ``turn/completed`` never arriving (timeout +
  ``turn/interrupt``).
* Heartbeat: fires when ``on_heartbeat`` is provided AND the idle time
  exceeds the heartbeat interval (driven by a stub clock).
* Version gate: parses ``codex --version`` output across success / too-low
  / unparseable / not-installed branches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from jarvis.execution import codex_action as ca

if TYPE_CHECKING:
    from collections.abc import Mapping


# ---------------------------------------------------------------------------
# FakeClient — a stand-in for CodexAppServerClient.
# ---------------------------------------------------------------------------


@dataclass
class FakeClient:
    """Mimic the constructor surface and the four methods the driver calls.

    Attributes:
        codex_bin / extra_args / env: captured constructor kwargs so the
            test can assert the 8 ``-c`` flags appeared in ``extra_args``.
        notifications: pre-canned FIFO queue. Each ``take_notification``
            call pops one (or returns ``None`` once exhausted).
        request_log: every ``method`` passed to ``.request(...)`` — used
            for assertions about ``thread/start`` + ``turn/start`` +
            ``turn/interrupt`` invocation order.
        initialize_raises / thread_start_raises / turn_start_raises:
            exception instances to raise from the matching method, or
            ``None`` for success.
        closed: set to ``True`` by ``close(...)``.
    """

    codex_bin: str | None = None
    extra_args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None
    notifications: list[dict[str, Any]] = field(default_factory=list)
    request_log: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    initialize_raises: Exception | None = None
    thread_start_raises: Exception | None = None
    turn_start_raises: Exception | None = None
    interrupt_raises: Exception | None = None
    thread_id: str = "tid-1"
    closed: bool = False

    def initialize(self, timeout: float = 10.0) -> dict[str, Any]:
        """Return a fake initialize response, or raise the seeded exception."""
        del timeout
        if self.initialize_raises is not None:
            raise self.initialize_raises
        return {"userAgent": "fake"}

    def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Record the request, raise on seeded failure modes, else echo a stub."""
        del timeout
        self.request_log.append((method, dict(params or {})))
        if method == "thread/start":
            if self.thread_start_raises is not None:
                raise self.thread_start_raises
            return {"threadId": self.thread_id}
        if method == "turn/start":
            if self.turn_start_raises is not None:
                raise self.turn_start_raises
            return {}
        if method == "turn/interrupt":
            if self.interrupt_raises is not None:
                raise self.interrupt_raises
            return {}
        return {}

    def take_notification(self, timeout: float = 0.0) -> dict[str, Any] | None:
        """Pop the next pre-canned notification, or ``None`` if drained."""
        del timeout
        if self.notifications:
            return self.notifications.pop(0)
        return None

    def close(self, timeout: float = 3.0) -> None:
        """Mark the client closed (no-op stand-in for subprocess teardown)."""
        del timeout
        self.closed = True


# ---------------------------------------------------------------------------
# Factories — builders for FakeClient instances + monkeypatch helpers.
# ---------------------------------------------------------------------------


def _patch_client(monkeypatch: pytest.MonkeyPatch, client_holder: list[FakeClient]) -> None:
    """Patch ``codex_action.CodexAppServerClient`` with a capturing fake.

    The fake's constructor stashes every instance into ``client_holder``
    so tests can assert against ``extra_args`` / ``env`` after the driver
    runs. Pre-seeded queue/failure-mode templates are copied off the
    holder's first slot before that slot is replaced with the live fake.
    """

    def _factory(
        codex_bin: str = "codex",
        extra_args: list[str] | None = None,
        env: dict[str, str] | None = None,
        **_: object,
    ) -> FakeClient:
        client = FakeClient(codex_bin=codex_bin, extra_args=list(extra_args or []), env=env)
        # Drain whatever queue the test pre-stashed onto the holder slot.
        if client_holder:
            template = client_holder[0]
            client.notifications = list(template.notifications)
            client.initialize_raises = template.initialize_raises
            client.thread_start_raises = template.thread_start_raises
            client.turn_start_raises = template.turn_start_raises
        client_holder.clear()
        client_holder.append(client)
        return client

    monkeypatch.setattr(ca, "CodexAppServerClient", _factory)


def _patch_diff_capture(monkeypatch: pytest.MonkeyPatch, text: str = "") -> None:
    """Stub the ``git diff`` capture to a fixed string so tests stay hermetic."""
    monkeypatch.setattr(ca, "_capture_diff", lambda _cwd: text)


def _stub_clock(monkeypatch: pytest.MonkeyPatch, ticks: list[float]) -> None:
    """Replace ``time.monotonic`` inside ``codex_action`` with a tick generator.

    Each call to ``time.monotonic()`` pops the next value from ``ticks``;
    once exhausted, the final value is reused. Useful for asserting
    heartbeat / timeout branches deterministically.
    """
    import contextlib  # noqa: PLC0415 - local helper, avoid leaking import at module scope

    iterator = iter(ticks)
    last: list[float] = [ticks[0]]

    def _now() -> float:
        with contextlib.suppress(StopIteration):
            last[0] = next(iterator)
        return last[0]

    monkeypatch.setattr(ca.time, "monotonic", _now)


# ---------------------------------------------------------------------------
# TOML escape helpers.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("hello", '"hello"'),
        ("path with spaces", '"path with spaces"'),
        ('with "double" quotes', '"with \\"double\\" quotes"'),
        ("with 'single' quotes", '"with \'single\' quotes"'),
        ("trailing back\\slash", '"trailing back\\\\slash"'),
        ("café-ünicode", '"café-ünicode"'),
        ("", '""'),
        ("/tmp/x", '"/tmp/x"'),
    ],
)
def test_toml_str_escapes_weird_paths(raw: str, expected: str) -> None:
    """``_toml_str`` escapes backslash and double-quote and wraps in ``"..."``."""
    assert ca._toml_str(raw) == expected  # noqa: SLF001 - test of private helper


def test_toml_list_quote_wraps_path() -> None:
    """``_toml_list_quote`` produces a TOML inline-array literal."""
    assert ca._toml_list_quote(Path("/tmp/x")) == '["/tmp/x"]'  # noqa: SLF001


def test_toml_list_quote_handles_weird_chars() -> None:
    """``_toml_list_quote`` escapes the inner string the same way as ``_toml_str``."""
    weird = Path('/tmp/has "quote"')
    assert ca._toml_list_quote(weird) == '["/tmp/has \\"quote\\""]'  # noqa: SLF001


# ---------------------------------------------------------------------------
# _build_extra_args — the 8 -c flag slice.
# ---------------------------------------------------------------------------


def test_build_extra_args_has_eight_c_flags(tmp_path: Path) -> None:
    """``_build_extra_args`` emits exactly 8 ``-c`` flag pairs (16 argv tokens)."""
    args = ca._build_extra_args(  # noqa: SLF001 - test of private helper
        cwd=tmp_path,
        model="gpt-5.5",
        reasoning_effort="xhigh",
    )
    assert args.count("-c") == 8
    assert len(args) == 16


def test_build_extra_args_carries_all_required_keys(tmp_path: Path) -> None:
    """All four MCP keys + the four sandbox keys appear in the argv slice."""
    args = ca._build_extra_args(  # noqa: SLF001
        cwd=tmp_path,
        model="gpt-5.5",
        reasoning_effort="xhigh",
    )
    joined = "\n".join(args)
    for key in (
        "model=gpt-5.5",
        "model_reasoning_effort=xhigh",
        "sandbox_mode=workspace-write",
        "sandbox_workspace_write.writable_roots=",
        "mcp_servers.jarvis-tools.command=",
        "mcp_servers.jarvis-tools.args=",
        "mcp_servers.jarvis-tools.startup_timeout_sec=30.0",
        "mcp_servers.jarvis-tools.tool_timeout_sec=600.0",
    ):
        assert key in joined, f"missing -c key: {key!r}"


# ---------------------------------------------------------------------------
# run_codex_action — happy path + capture.
# ---------------------------------------------------------------------------


def test_run_codex_action_happy_path_captures_submit_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One ``submit_report`` tool-call in the stream surfaces in the result."""
    submit_args = {"status": "ok", "summary": "did the thing"}
    template = FakeClient(
        notifications=[
            {"method": "item/text", "params": {"text": "thinking..."}},
            {
                "method": "item/tool_call",
                "params": {"toolName": "submit_report", "arguments": submit_args},
            },
            {
                "method": "turn/completed",
                "params": {
                    "turnId": "turn-abc",
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                },
            },
        ],
    )
    holder: list[FakeClient] = [template]
    _patch_client(monkeypatch, holder)
    _patch_diff_capture(monkeypatch, text="diff --git a/x b/x\n")

    result = ca.run_codex_action(
        task_goal="implement feature X",
        cwd=tmp_path,
        timeout_s=5.0,
    )

    assert result.error is None
    assert result.interrupted is False
    assert result.submit_report == submit_args
    assert result.submit_report_calls == (submit_args,)
    assert result.turn_id == "turn-abc"
    assert result.tokens_in == 100
    assert result.tokens_out == 50
    assert result.diff_text == "diff --git a/x b/x\n"
    assert result.diff_path is None
    assert "thinking..." in result.final_text

    # FakeClient instance is captured in holder[0] post-construction.
    client = holder[0]
    assert client.codex_bin == "codex"
    assert client.extra_args.count("-c") == 8
    # The 8 spec keys appear in the captured extra_args.
    joined = "\n".join(client.extra_args)
    assert "mcp_servers.jarvis-tools.command=" in joined
    assert "mcp_servers.jarvis-tools.args=" in joined
    assert "mcp_servers.jarvis-tools.startup_timeout_sec=30.0" in joined
    assert "mcp_servers.jarvis-tools.tool_timeout_sec=600.0" in joined
    assert client.closed is True
    # Request order: thread/start -> turn/start (no interrupt on happy path).
    methods = [m for m, _ in client.request_log]
    assert methods == ["thread/start", "turn/start"]


def test_run_codex_action_captures_multiple_submit_report_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple ``submit_report`` calls all surface; ``submit_report`` is the first."""
    call_one = {"status": "partial", "summary": "first cut"}
    call_two = {"status": "ok", "summary": "final"}
    submit_one = {
        "method": "item/tool_call",
        "params": {"toolName": "submit_report", "arguments": call_one},
    }
    other_call = {
        "method": "item/tool_call",
        "params": {"toolName": "other_tool", "arguments": {}},
    }
    submit_two = {
        "method": "item/tool_call",
        "params": {"toolName": "submit_report", "arguments": call_two},
    }
    template = FakeClient(
        notifications=[
            submit_one,
            other_call,
            submit_two,
            {"method": "turn/completed", "params": {"turnId": "t"}},
        ],
    )
    _patch_client(monkeypatch, [template])
    _patch_diff_capture(monkeypatch)

    result = ca.run_codex_action(task_goal="t", cwd=tmp_path, timeout_s=5.0)

    assert result.submit_report == call_one
    assert result.submit_report_calls == (call_one, call_two)


def test_run_codex_action_tolerates_snake_case_field_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``tool_name`` / ``args`` / ``turn_id`` are accepted as protocol variants."""
    submit_args = {"status": "ok", "summary": "snake"}
    template = FakeClient(
        notifications=[
            {
                "method": "item/tool_call",
                "params": {"tool_name": "submit_report", "args": submit_args},
            },
            {"method": "turn/completed", "params": {"turn_id": "tn-1"}},
        ],
    )
    _patch_client(monkeypatch, [template])
    _patch_diff_capture(monkeypatch)

    result = ca.run_codex_action(task_goal="t", cwd=tmp_path, timeout_s=5.0)

    assert result.submit_report == submit_args
    assert result.turn_id == "tn-1"


def test_run_codex_action_no_submit_report_call_leaves_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``submit_report`` tool-call → ``submit_report is None``, empty tuple."""
    template = FakeClient(
        notifications=[
            {"method": "item/text", "params": {"text": "no tool call here"}},
            {"method": "turn/completed", "params": {}},
        ],
    )
    _patch_client(monkeypatch, [template])
    _patch_diff_capture(monkeypatch)

    result = ca.run_codex_action(task_goal="t", cwd=tmp_path, timeout_s=5.0)

    assert result.error is None
    assert result.submit_report is None
    assert result.submit_report_calls == ()


# ---------------------------------------------------------------------------
# Failure paths.
# ---------------------------------------------------------------------------


def test_run_codex_action_initialize_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``initialize`` raising → structured ``codex_initialize_failed`` error."""
    template = FakeClient(initialize_raises=RuntimeError("boom"))
    _patch_client(monkeypatch, [template])
    _patch_diff_capture(monkeypatch)

    result = ca.run_codex_action(task_goal="t", cwd=tmp_path, timeout_s=1.0)

    assert result.error is not None
    assert result.error.startswith("codex_initialize_failed:")
    assert result.interrupted is False
    assert result.submit_report is None


def test_run_codex_action_thread_start_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``thread/start`` raising → structured ``codex_thread_start_failed`` error."""
    template = FakeClient(thread_start_raises=RuntimeError("rpc-fail"))
    _patch_client(monkeypatch, [template])
    _patch_diff_capture(monkeypatch)

    result = ca.run_codex_action(task_goal="t", cwd=tmp_path, timeout_s=1.0)

    assert result.error is not None
    assert result.error.startswith("codex_thread_start_failed:")
    assert result.interrupted is False


def test_run_codex_action_turn_start_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``turn/start`` raising → structured ``codex_turn_start_failed`` error."""
    template = FakeClient(turn_start_raises=RuntimeError("rpc-fail"))
    _patch_client(monkeypatch, [template])
    _patch_diff_capture(monkeypatch)

    result = ca.run_codex_action(task_goal="t", cwd=tmp_path, timeout_s=1.0)

    assert result.error is not None
    assert result.error.startswith("codex_turn_start_failed:")
    assert result.interrupted is False


def test_run_codex_action_timeout_sends_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``turn/completed`` before deadline → interrupt + ``codex_turn_timeout``."""
    template = FakeClient(notifications=[])  # never completes
    holder = [template]
    _patch_client(monkeypatch, holder)
    _patch_diff_capture(monkeypatch)

    # Drive a deterministic clock: start at 0, then jump past the deadline
    # on the first loop iteration so the timeout branch fires immediately.
    _stub_clock(monkeypatch, ticks=[0.0, 100.0, 100.0, 100.0])

    result = ca.run_codex_action(task_goal="t", cwd=tmp_path, timeout_s=1.0)

    assert result.interrupted is True
    assert result.error == "codex_turn_timeout"
    methods = [m for m, _ in holder[0].request_log]
    assert "turn/interrupt" in methods


def test_run_codex_action_interrupt_failure_does_not_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the interrupt RPC itself raises, the driver still returns cleanly."""
    template = FakeClient(notifications=[], interrupt_raises=RuntimeError("interrupt-fail"))
    _patch_client(monkeypatch, [template])
    _patch_diff_capture(monkeypatch)
    _stub_clock(monkeypatch, ticks=[0.0, 100.0, 100.0])

    result = ca.run_codex_action(task_goal="t", cwd=tmp_path, timeout_s=1.0)

    assert result.interrupted is True
    assert result.error == "codex_turn_timeout"


# ---------------------------------------------------------------------------
# Heartbeat.
# ---------------------------------------------------------------------------


def test_run_codex_action_heartbeat_fires(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``on_heartbeat`` fires after 30 s wall-clock idle, then again after another 30 s."""
    # Stream: a few idle ticks then turn/completed. The clock skips 30 s
    # per call so the heartbeat predicate trips each loop iteration.
    template = FakeClient(
        notifications=[
            None,  # ticks 1 — heartbeat 1 at ~30s
            None,  # ticks 2 — heartbeat 2 at ~60s
            {"method": "turn/completed", "params": {}},
        ],
    )
    # ``take_notification`` returns the next pop; ``None`` is the
    # sentinel meaning "no notification this tick". The FakeClient pops
    # None entries too, so they translate to "idle" in the driver.
    _patch_client(monkeypatch, [template])
    _patch_diff_capture(monkeypatch)

    # Clock jumps: 0 (start), 0 (deadline calc), 30 (loop iter 1 now),
    # 30 (heartbeat-fire branch), 60 (loop iter 2 now), 60 (hb 2), 90...
    _stub_clock(
        monkeypatch,
        ticks=[0.0, 0.0, 30.0, 30.0, 60.0, 60.0, 90.0, 90.0, 90.0],
    )

    heartbeats: list[dict[str, Any]] = []
    result = ca.run_codex_action(
        task_goal="t",
        cwd=tmp_path,
        timeout_s=600.0,
        on_heartbeat=heartbeats.append,
    )

    assert result.error is None
    assert len(heartbeats) >= 1
    for hb in heartbeats:
        assert "summary" in hb
        assert "elapsed_ms" in hb
        assert "last_item_summary" in hb


# ---------------------------------------------------------------------------
# Version gate.
# ---------------------------------------------------------------------------


def _make_completed_proc(stdout: str = "", stderr: str = "") -> MagicMock:
    """Return a MagicMock standing in for ``subprocess.CompletedProcess``."""
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


def test_ensure_codex_version_supported_accepts_min_version() -> None:
    """Exactly ``0.125.0`` is accepted (the floor)."""
    with patch.object(
        ca.subprocess,
        "run",
        return_value=_make_completed_proc(stdout="codex-cli 0.125.0\n"),
    ):
        ca.ensure_codex_version_supported()  # no raise


def test_ensure_codex_version_supported_accepts_higher_version() -> None:
    """Any version above the floor is accepted."""
    with patch.object(
        ca.subprocess,
        "run",
        return_value=_make_completed_proc(stdout="codex-cli 0.130.5\n"),
    ):
        ca.ensure_codex_version_supported()  # no raise


def test_ensure_codex_version_supported_rejects_too_low() -> None:
    """``0.124.99`` is rejected."""
    with patch.object(
        ca.subprocess,
        "run",
        return_value=_make_completed_proc(stdout="codex-cli 0.124.99\n"),
    ), pytest.raises(ca.CodexVersionTooLow):
        ca.ensure_codex_version_supported()


def test_ensure_codex_version_supported_rejects_unparseable() -> None:
    """Output that doesn't contain a version triplet raises."""
    with patch.object(
        ca.subprocess,
        "run",
        return_value=_make_completed_proc(stdout="codex-cli unknown\n"),
    ), pytest.raises(ca.CodexVersionTooLow):
        ca.ensure_codex_version_supported()


def test_ensure_codex_version_supported_rejects_missing_binary() -> None:
    """``FileNotFoundError`` from subprocess.run surfaces as ``CodexVersionTooLow``."""
    with patch.object(
        ca.subprocess,
        "run",
        side_effect=FileNotFoundError("no such file"),
    ), pytest.raises(ca.CodexVersionTooLow):
        ca.ensure_codex_version_supported(codex_bin="/nope/codex")


def test_ensure_codex_version_supported_uses_stderr_too() -> None:
    """Some codex builds print the version on stderr; we accept either."""
    with patch.object(
        ca.subprocess,
        "run",
        return_value=_make_completed_proc(stdout="", stderr="codex-cli 0.125.0\n"),
    ):
        ca.ensure_codex_version_supported()  # no raise


# ---------------------------------------------------------------------------
# diff capture stub.
# ---------------------------------------------------------------------------


def test_capture_diff_returns_empty_on_git_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_capture_diff`` swallows ``FileNotFoundError`` and returns ``""``."""

    def _raise(*_args: object, **_kwargs: object) -> None:
        msg = "no git"
        raise FileNotFoundError(msg)

    monkeypatch.setattr(ca.subprocess, "run", _raise)
    assert ca._capture_diff(Path("/tmp")) == ""  # noqa: SLF001


def test_capture_diff_returns_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a successful ``git diff`` call, the stdout text is returned verbatim."""
    proc = _make_completed_proc(stdout="diff --git a/x b/x\n+line\n")
    monkeypatch.setattr(ca.subprocess, "run", lambda *_a, **_k: proc)
    assert ca._capture_diff(Path("/tmp")) == "diff --git a/x b/x\n+line\n"  # noqa: SLF001
