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
* Driver happy path: all 11 ``-c`` flags appear in ``extra_args``;
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

import subprocess
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
    notifications: list[dict[str, Any] | None] = field(default_factory=list)
    server_requests: list[dict[str, Any]] = field(default_factory=list)
    request_log: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    respond_log: list[tuple[object, dict[str, Any]]] = field(default_factory=list)
    respond_error_log: list[tuple[object, int, str]] = field(default_factory=list)
    initialize_raises: Exception | None = None
    thread_start_raises: Exception | None = None
    turn_start_raises: Exception | None = None
    interrupt_raises: Exception | None = None
    thread_id: str = "tid-1"
    closed: bool = False
    alive: bool = True

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
            return {"thread": {"id": self.thread_id}}
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

    def take_server_request(self, timeout: float = 0.0) -> dict[str, Any] | None:
        """Pop the next pre-canned server-initiated request, or ``None``."""
        del timeout
        if self.server_requests:
            return self.server_requests.pop(0)
        return None

    def respond(self, request_id: object, result: dict[str, Any]) -> None:
        """Record a reply sent to a server-initiated request."""
        self.respond_log.append((request_id, dict(result)))

    def respond_error(
        self,
        request_id: object,
        code: int,
        message: str,
        data: object = None,
    ) -> None:
        """Record an error reply sent to a server-initiated request."""
        del data
        self.respond_error_log.append((request_id, code, message))

    def close(self, timeout: float = 3.0) -> None:
        """Mark the client closed (no-op stand-in for subprocess teardown)."""
        del timeout
        self.closed = True

    def is_alive(self) -> bool:
        """Stand-in for the subprocess liveness probe (``_proc.poll() is None``)."""
        return self.alive


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
            client.server_requests = list(template.server_requests)
            client.initialize_raises = template.initialize_raises
            client.thread_start_raises = template.thread_start_raises
            client.turn_start_raises = template.turn_start_raises
            client.alive = template.alive
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
# _build_extra_args — the 11 -c flag slice.
# ---------------------------------------------------------------------------


def test_build_extra_args_has_eleven_c_flags(tmp_path: Path) -> None:
    """``_build_extra_args`` emits exactly 11 ``-c`` flag pairs (22 argv tokens)."""
    args = ca._build_extra_args(  # noqa: SLF001 - test of private helper
        cwd=tmp_path,
        model="gpt-5.5",
        reasoning_effort="xhigh",
    )
    assert args.count("-c") == 11
    assert len(args) == 22


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
        "approval_policy=never",
        "features.enable_mcp_apps=true",
        "features.builtin_mcp=true",
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
    assert client.extra_args.count("-c") == 11
    # The 11 spec keys appear in the captured extra_args.
    joined = "\n".join(client.extra_args)
    assert "mcp_servers.jarvis-tools.command=" in joined
    assert "mcp_servers.jarvis-tools.args=" in joined
    assert "mcp_servers.jarvis-tools.startup_timeout_sec=30.0" in joined
    assert "mcp_servers.jarvis-tools.tool_timeout_sec=600.0" in joined
    assert client.closed is True
    # Request order: thread/start -> turn/start (no interrupt on happy path).
    methods = [m for m, _ in client.request_log]
    assert methods == ["thread/start", "turn/start"]


def test_run_codex_action_thread_start_cwd_matches_repo_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """J3: the ``thread/start`` ``cwd`` param equals the ``cwd`` arg byte-for-byte.

    The cwd lives on the Codex ``thread/start`` JSON-RPC request, which is
    internal to the subprocess and never mirrored into the Event Log — so
    the Tier-2 scenario stub
    (``test_real_codex_flagship.py::test_j3_thread_start_cwd_matches_repo_path``)
    defers here, where the FakeClient ``request_log`` makes the outgoing
    param observable without spawning a real Codex.
    """
    template = FakeClient(
        notifications=[
            {
                "method": "turn/completed",
                "params": {
                    "turnId": "turn-j3",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            },
        ],
    )
    holder: list[FakeClient] = [template]
    _patch_client(monkeypatch, holder)
    _patch_diff_capture(monkeypatch, text="diff --git a/x b/x\n")

    ca.run_codex_action(task_goal="t", cwd=tmp_path, timeout_s=5.0)

    client = holder[0]
    thread_start_params = [
        params for method, params in client.request_log if method == "thread/start"
    ]
    assert thread_start_params, "thread/start was never sent"
    assert thread_start_params[0]["cwd"] == str(tmp_path)


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


def test_run_codex_action_dead_subprocess_maps_to_crash_not_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subprocess that dies before ``turn/completed`` → ``codex_subprocess_crashed``.

    Without crash detection the poll loop would spin until the deadline
    and mislabel a dead Codex subprocess as ``codex_turn_timeout`` (and
    burn the full budget first). The loop must instead notice
    ``is_alive() is False`` once the notification queue is drained and
    bail with ``error="codex_subprocess_crashed"`` (J8 / ADR-0002
    Negative-path appendix). ``timeout_s`` is tiny so a regression fails
    fast instead of hanging.
    """
    # No turn/completed notification + the subprocess reports dead.
    template = FakeClient(notifications=[], alive=False)
    _patch_client(monkeypatch, [template])
    _patch_diff_capture(monkeypatch)

    result = ca.run_codex_action(task_goal="t", cwd=tmp_path, timeout_s=1.0)

    assert result.error == "codex_subprocess_crashed"
    assert result.interrupted is False


# ---------------------------------------------------------------------------
# CODEX_HOME isolation (P-0009).
# ---------------------------------------------------------------------------


def test_run_codex_action_injects_isolated_codex_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spawn env must carry CODEX_HOME pointing at a per-spawn empty dir.

    P-0009: the spawned ``codex app-server`` inherits ``CODEX_HOME``
    from its env. Default ``~/.codex/`` causes Allen's personal
    ``AGENTS.md`` (which imports ``RTK.md``) to contaminate every
    shell command the worker issues. The fix is to inject a per-spawn
    empty ``CODEX_HOME`` so user-local config cannot leak into the
    worker. This test pins the structural property without spawning
    real Codex.
    """
    template = FakeClient(
        notifications=[
            # Zero-item streams are now classified codex_empty_turn
            # (dead-auth shape), so seed one item before turn/completed.
            {"method": "item/text", "params": {"text": "working"}},
            {"method": "turn/completed", "params": {}},
        ],
    )
    holder: list[FakeClient] = [template]
    _patch_client(monkeypatch, holder)
    _patch_diff_capture(monkeypatch)

    result = ca.run_codex_action(task_goal="t", cwd=tmp_path, timeout_s=5.0)

    assert result.error is None
    client = holder[0]
    assert client.env is not None
    codex_home = client.env.get("CODEX_HOME")
    assert codex_home, "CODEX_HOME must be set in the spawn env"
    # Critical: must NOT be the user-default ~/.codex.
    assert Path(codex_home).resolve() != Path("~/.codex").expanduser().resolve()
    # And it must be a real, currently-empty directory at spawn time.
    # (It is removed in the _result closure on close(); we only assert the
    # prefix here, since the dir is gone by the time we get here.)
    assert ca._CODEX_HOME_PREFIX in codex_home  # noqa: SLF001 - test of private constant


def test_run_codex_action_respects_caller_provided_codex_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If caller pre-sets ``CODEX_HOME`` on ``env``, the driver must not override it.

    Lets future callers point at a stable hermetic dir (e.g. for replay
    debugging) without the driver creating + tearing down a temp dir
    on every spawn.
    """
    template = FakeClient(
        notifications=[
            # See note in the test above — zero-item streams are now
            # classified codex_empty_turn, so seed one item.
            {"method": "item/text", "params": {"text": "working"}},
            {"method": "turn/completed", "params": {}},
        ],
    )
    holder: list[FakeClient] = [template]
    _patch_client(monkeypatch, holder)
    _patch_diff_capture(monkeypatch)

    explicit_home = str(tmp_path / "explicit-codex-home")
    result = ca.run_codex_action(
        task_goal="t",
        cwd=tmp_path,
        timeout_s=5.0,
        env={"CODEX_HOME": explicit_home},
    )

    assert result.error is None
    client = holder[0]
    assert client.env is not None
    assert client.env.get("CODEX_HOME") == explicit_home


def test_run_codex_action_ignores_ambient_codex_home_when_env_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ambient parent-process ``CODEX_HOME`` must NOT defeat per-spawn isolation.

    Regression: pre-fix the override gate read ``env_dict.get("CODEX_HOME")``
    where ``env_dict`` was ``os.environ.copy()`` when the caller passed
    ``env=None``. An ambient ``CODEX_HOME`` (e.g. exported in the parent
    shell of the calling agent) would then skip the tempdir branch and
    inherit the ambient home unseeded — defeating both P-0009
    (instruction contamination via the parent's ``AGENTS.md``) and B-0004
    (auth.json seeding skipped because no tempdir was created). The fix
    narrows the override predicate to an explicit ``env={CODEX_HOME: ...}``
    argument only; only the ``env`` parameter counts.
    """
    poison_home = tmp_path / "ambient-poison"
    poison_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(poison_home))

    template = FakeClient(
        notifications=[
            # Zero-item streams are now classified codex_empty_turn
            # (dead-auth shape), so seed one item before turn/completed.
            {"method": "item/text", "params": {"text": "working"}},
            {"method": "turn/completed", "params": {}},
        ],
    )
    holder: list[FakeClient] = [template]
    _patch_client(monkeypatch, holder)
    _patch_diff_capture(monkeypatch)

    result = ca.run_codex_action(task_goal="t", cwd=tmp_path, timeout_s=5.0)

    assert result.error is None
    client = holder[0]
    assert client.env is not None
    codex_home = client.env.get("CODEX_HOME")
    assert codex_home, "CODEX_HOME must be set in the spawn env"
    # Critical: ambient poison must be ignored, not propagated to the worker.
    assert codex_home != str(poison_home)
    # And the spawned worker must get a fresh isolation tempdir.
    assert ca._CODEX_HOME_PREFIX in codex_home  # noqa: SLF001 — test of private constant


def test_run_codex_action_seeds_auth_json_into_isolated_codex_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-0004: per-spawn CODEX_HOME must contain a copy of ``~/.codex/auth.json``.

    Codex 0.130's ``responses_websocket`` transport reads credentials from
    ``$CODEX_HOME/auth.json``, not from ``OPENAI_API_KEY``. Without this
    seed, the isolated tmpdir is empty and every request 401s
    (live-verified: 7 retries, zero tool calls, empty diff).
    """
    fake_home = tmp_path / "home"
    (fake_home / ".codex").mkdir(parents=True)
    fake_auth = fake_home / ".codex" / "auth.json"
    fake_auth.write_text('{"OPENAI_API_KEY":"sk-test"}')
    monkeypatch.setenv("HOME", str(fake_home))

    copy_calls: list[tuple[str, str]] = []

    def recording_copy(src: object, dst: object) -> None:
        copy_calls.append((str(src), str(dst)))

    monkeypatch.setattr("jarvis.execution.codex_action.shutil.copy2", recording_copy)

    template = FakeClient(
        notifications=[
            # Zero-item streams are now classified codex_empty_turn
            # (dead-auth shape), so seed one item before turn/completed.
            {"method": "item/text", "params": {"text": "working"}},
            {"method": "turn/completed", "params": {}},
        ],
    )
    _patch_client(monkeypatch, [template])
    _patch_diff_capture(monkeypatch)

    result = ca.run_codex_action(task_goal="t", cwd=tmp_path, timeout_s=5.0)

    assert result.error is None
    assert len(copy_calls) == 1
    src, dst = copy_calls[0]
    assert src == str(fake_auth)
    assert dst.endswith("/auth.json")
    assert ca._CODEX_HOME_PREFIX in dst  # noqa: SLF001 — test of private constant


def test_run_codex_action_skips_auth_json_seed_when_source_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``~/.codex/auth.json`` -> silent skip, driver must not raise.

    Keeps the failure mode identical to pre-B-0004 behavior on fresh
    machines: the worker may 401, but the driver itself returns cleanly.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()  # No .codex/ underneath.
    monkeypatch.setenv("HOME", str(fake_home))

    copy_calls: list[tuple[str, str]] = []

    def recording_copy(src: object, dst: object) -> None:
        copy_calls.append((str(src), str(dst)))

    monkeypatch.setattr("jarvis.execution.codex_action.shutil.copy2", recording_copy)

    template = FakeClient(
        notifications=[
            # Zero-item streams are now classified codex_empty_turn
            # (dead-auth shape), so seed one item before turn/completed.
            {"method": "item/text", "params": {"text": "working"}},
            {"method": "turn/completed", "params": {}},
        ],
    )
    _patch_client(monkeypatch, [template])
    _patch_diff_capture(monkeypatch)

    result = ca.run_codex_action(task_goal="t", cwd=tmp_path, timeout_s=5.0)

    assert result.error is None
    assert copy_calls == []


def test_run_codex_action_skips_auth_seed_when_caller_provides_codex_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-set ``CODEX_HOME`` -> driver does not seed auth.json (caller's job).

    The pre-set-home escape hatch is for callers managing their own home
    dir; auto-seeding their auth.json would be a surprise side effect.
    """
    fake_home = tmp_path / "home"
    (fake_home / ".codex").mkdir(parents=True)
    (fake_home / ".codex" / "auth.json").write_text("{}")
    monkeypatch.setenv("HOME", str(fake_home))

    copy_calls: list[tuple[str, str]] = []

    def recording_copy(src: object, dst: object) -> None:
        copy_calls.append((str(src), str(dst)))

    monkeypatch.setattr("jarvis.execution.codex_action.shutil.copy2", recording_copy)

    template = FakeClient(
        notifications=[
            # Zero-item streams are now classified codex_empty_turn
            # (dead-auth shape), so seed one item before turn/completed.
            {"method": "item/text", "params": {"text": "working"}},
            {"method": "turn/completed", "params": {}},
        ],
    )
    _patch_client(monkeypatch, [template])
    _patch_diff_capture(monkeypatch)

    explicit_home = str(tmp_path / "explicit-codex-home")
    result = ca.run_codex_action(
        task_goal="t",
        cwd=tmp_path,
        timeout_s=5.0,
        env={"CODEX_HOME": explicit_home},
    )

    assert result.error is None
    assert copy_calls == []


# ---------------------------------------------------------------------------
# _extract_thread_id — protocol-parse semantics (B-0002).
# ---------------------------------------------------------------------------


def test_extract_thread_id_prefers_nested_codex_0130_shape() -> None:
    """Codex 0.130 returns ``{"thread": {"id": ...}}`` — must be preferred."""
    result = {"thread": {"id": "tid-nested"}, "threadId": "tid-flat-stale"}
    assert ca._extract_thread_id(result) == "tid-nested"  # noqa: SLF001 — protocol-parse helper is module-private by design


def test_extract_thread_id_falls_back_to_flat_legacy_shapes() -> None:
    """Older flat shapes still parse if the nested ``thread`` envelope is absent."""
    assert ca._extract_thread_id({"threadId": "tid-flat"}) == "tid-flat"  # noqa: SLF001 — protocol-parse helper is module-private by design
    assert ca._extract_thread_id({"thread_id": "tid-snake"}) == "tid-snake"  # noqa: SLF001 — protocol-parse helper is module-private by design


def test_extract_thread_id_raises_on_empty_or_missing_id() -> None:
    """Empty dict, or ``thread`` envelope missing ``id``, must raise ``_ProtocolError``."""
    with pytest.raises(ca._ProtocolError, match="thread/start response missing threadId"):  # noqa: SLF001 — exception is module-private by design
        ca._extract_thread_id({})  # noqa: SLF001 — protocol-parse helper is module-private by design
    with pytest.raises(ca._ProtocolError, match="thread/start response missing threadId"):  # noqa: SLF001 — exception is module-private by design
        ca._extract_thread_id({"thread": {}})  # noqa: SLF001 — protocol-parse helper is module-private by design


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
            {"method": "item/text", "params": {"text": "working"}},
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


def test_run_codex_action_respects_heartbeat_interval_param(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A custom ``heartbeat_interval_s`` drives the cadence, not the 30 s constant.

    The J4 live seam lowers the interval so a real turn emits a
    ``worker.heartbeat`` in seconds. This proves the poll loop honours the
    *parameter*, not the ``_HEARTBEAT_INTERVAL_S`` module constant: with a
    2 s interval and a clock that only advances ~5 s per idle tick (never
    reaching 30 s), at least one heartbeat must still fire.
    """
    template = FakeClient(
        notifications=[
            None,  # idle tick 1 — now ~5 s; fires at interval=2, not at 30
            None,  # idle tick 2 — now ~10 s
            {"method": "item/text", "params": {"text": "working"}},
            {"method": "turn/completed", "params": {}},
        ],
    )
    _patch_client(monkeypatch, [template])
    _patch_diff_capture(monkeypatch)

    # Small steps so the 30 s default would NEVER fire: 0 (start),
    # 5 (iter1 now), 5 (hb branch), 10 (iter2 now), 10 (hb branch), 15...
    _stub_clock(
        monkeypatch,
        ticks=[0.0, 0.0, 5.0, 5.0, 10.0, 10.0, 15.0, 15.0, 15.0],
    )

    heartbeats: list[dict[str, Any]] = []
    result = ca.run_codex_action(
        task_goal="t",
        cwd=tmp_path,
        timeout_s=600.0,
        on_heartbeat=heartbeats.append,
        heartbeat_interval_s=2.0,
    )

    assert result.error is None
    assert len(heartbeats) >= 1, (
        "heartbeat_interval_s=2.0 must fire within a 5 s idle tick; "
        "the loop is using the 30 s constant instead of the parameter"
    )


# ---------------------------------------------------------------------------
# B-0013 — Codex 0.130 item/completed mcpToolCall capture + elicitation drain.
# ---------------------------------------------------------------------------


def test_run_codex_action_captures_submit_report_from_item_completed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex 0.130 schema: ``item/completed`` with ``type=mcpToolCall`` surfaces submit_report."""
    submit_args = {
        "status": "ok",
        "summary": "did the thing",
        "changed_files": ["a.py"],
        "commands_run": ["uv run pytest"],
    }
    template = FakeClient(
        notifications=[
            {
                "method": "item/started",
                "params": {
                    "item": {
                        "type": "mcpToolCall",
                        "id": "call_xyz",
                        "server": "jarvis-tools",
                        "tool": "submit_report",
                        "status": "inProgress",
                        "arguments": submit_args,
                        "result": None,
                    }
                },
            },
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "mcpToolCall",
                        "id": "call_xyz",
                        "server": "jarvis-tools",
                        "tool": "submit_report",
                        "status": "completed",
                        "arguments": submit_args,
                        "result": {
                            "content": [
                                {"type": "text", "text": "submit_report accepted"}
                            ],
                            "structuredContent": submit_args,
                            "_meta": None,
                        },
                    }
                },
            },
            {
                "method": "turn/completed",
                "params": {
                    "turnId": "turn-0130",
                    "usage": {"input_tokens": 42, "output_tokens": 7},
                },
            },
        ],
    )
    _patch_client(monkeypatch, [template])
    _patch_diff_capture(monkeypatch)

    result = ca.run_codex_action(task_goal="t", cwd=tmp_path, timeout_s=5.0)

    assert result.error is None
    assert result.submit_report == submit_args
    assert result.submit_report_calls == (submit_args,)
    assert result.turn_id == "turn-0130"


def test_run_codex_action_ignores_item_completed_for_other_mcp_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``item/completed`` for an mcpToolCall that is NOT submit_report is skipped."""
    template = FakeClient(
        notifications=[
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "mcpToolCall",
                        "server": "jarvis-tools",
                        "tool": "some_other_tool",
                        "status": "completed",
                        "arguments": {"k": "v"},
                        "result": {"structuredContent": {}},
                    }
                },
            },
            {"method": "item/text", "params": {"text": "working"}},
            {"method": "turn/completed", "params": {}},
        ],
    )
    _patch_client(monkeypatch, [template])
    _patch_diff_capture(monkeypatch)

    result = ca.run_codex_action(task_goal="t", cwd=tmp_path, timeout_s=5.0)

    assert result.error is None
    assert result.submit_report is None
    assert result.submit_report_calls == ()


def test_run_codex_action_item_completed_non_mcp_falls_back_to_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``item/completed`` without an mcpToolCall item still surfaces text into ``final_text``."""
    template = FakeClient(
        notifications=[
            {
                "method": "item/completed",
                "params": {"text": "hello from completed item"},
            },
            {"method": "item/text", "params": {"text": "working"}},
            {"method": "turn/completed", "params": {}},
        ],
    )
    _patch_client(monkeypatch, [template])
    _patch_diff_capture(monkeypatch)

    result = ca.run_codex_action(task_goal="t", cwd=tmp_path, timeout_s=5.0)

    assert result.error is None
    assert "hello from completed item" in result.final_text


def test_run_codex_action_auto_accepts_mcp_elicitation_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-0013: pending ``mcpServer/elicitation/request`` is auto-accepted before tool dispatch.

    Models Codex 0.130 sending an elicitation request immediately before
    the submit_report tool actually executes. The driver must reply
    ``{action: accept, content: None, _meta: None}`` so the tool
    proceeds; otherwise the turn hangs to its 60s budget.
    """
    template = FakeClient(
        server_requests=[
            {
                "id": 42,
                "method": "mcpServer/elicitation/request",
                "params": {
                    "threadId": "t1",
                    "turnId": "turn-1",
                    "serverName": "jarvis-tools",
                    "mode": "form",
                    "_meta": {
                        "codex_approval_kind": "mcp_tool_call",
                        "tool_description": "submit_report",
                    },
                },
            },
        ],
        notifications=[
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "mcpToolCall",
                        "server": "jarvis-tools",
                        "tool": "submit_report",
                        "status": "completed",
                        "arguments": {"status": "ok", "summary": "done"},
                        "result": {"structuredContent": {}},
                    }
                },
            },
            {"method": "turn/completed", "params": {}},
        ],
    )
    holder: list[FakeClient] = [template]
    _patch_client(monkeypatch, holder)
    _patch_diff_capture(monkeypatch)

    result = ca.run_codex_action(task_goal="t", cwd=tmp_path, timeout_s=5.0)

    assert result.error is None
    assert result.submit_report == {"status": "ok", "summary": "done"}
    # Driver must have responded to the elicitation with action=accept.
    client = holder[0]
    assert len(client.respond_log) == 1
    req_id, payload = client.respond_log[0]
    assert req_id == 42
    assert payload == {"action": "accept", "content": None, "_meta": None}
    # No error replies on the happy path.
    assert client.respond_error_log == []


def test_run_codex_action_auto_approves_approval_server_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any server request whose method contains ``approval`` is auto-approved."""
    template = FakeClient(
        server_requests=[
            {
                "id": 7,
                "method": "exec/applyPatch/approval",
                "params": {"patch": "diff..."},
            },
        ],
        notifications=[
            # Zero-item streams are now classified codex_empty_turn
            # (dead-auth shape), so seed one item before turn/completed.
            {"method": "item/text", "params": {"text": "working"}},
            {"method": "turn/completed", "params": {}},
        ],
    )
    holder: list[FakeClient] = [template]
    _patch_client(monkeypatch, holder)
    _patch_diff_capture(monkeypatch)

    result = ca.run_codex_action(task_goal="t", cwd=tmp_path, timeout_s=5.0)

    assert result.error is None
    client = holder[0]
    assert client.respond_log == [(7, {"decision": "approve"})]
    assert client.respond_error_log == []


def test_run_codex_action_replies_method_not_found_for_unknown_server_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown server-initiated requests get an explicit method-not-found error reply.

    Codex would otherwise sit waiting for a response that never comes.
    We tell it explicitly that we cannot service the request so the
    transport can fail-fast / surface the failure.
    """
    template = FakeClient(
        server_requests=[
            {"id": 99, "method": "totally/unknown", "params": {}},
        ],
        notifications=[
            # Zero-item streams are now classified codex_empty_turn
            # (dead-auth shape), so seed one item before turn/completed.
            {"method": "item/text", "params": {"text": "working"}},
            {"method": "turn/completed", "params": {}},
        ],
    )
    holder: list[FakeClient] = [template]
    _patch_client(monkeypatch, holder)
    _patch_diff_capture(monkeypatch)

    result = ca.run_codex_action(task_goal="t", cwd=tmp_path, timeout_s=5.0)

    assert result.error is None
    client = holder[0]
    assert client.respond_log == []
    assert len(client.respond_error_log) == 1
    req_id, code, msg = client.respond_error_log[0]
    assert req_id == 99
    assert code == -32601
    assert "totally/unknown" in msg


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


_GIT_USER_FLAGS = ["-c", "user.email=t@t", "-c", "user.name=t"]


def _init_repo_with_tracked_file(repo: Path) -> None:
    """Create a real git repo with one committed tracked file ``a.txt``.

    Real git (no mocks): untracked-file inclusion is git-internal, so a
    mock would diverge from production — same rationale as
    ``tests/unit/test_diff_capture.py``.
    """
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603 — fixed git argv, no shell, test fixture.
        ["git", "init", "-q", "-b", "main", str(repo)],  # noqa: S607 — git on PATH by design.
        check=True,
        capture_output=True,
    )
    (repo / "a.txt").write_text("base\n")
    subprocess.run(  # noqa: S603 — fixed git argv, no shell, test fixture.
        ["git", "-C", str(repo), *_GIT_USER_FLAGS, "add", "a.txt"],  # noqa: S607 — git on PATH by design.
        check=True,
        capture_output=True,
    )
    subprocess.run(  # noqa: S603 — fixed git argv, no shell, test fixture.
        ["git", "-C", str(repo), *_GIT_USER_FLAGS, "commit", "-q", "-m", "init"],  # noqa: S607 — git on PATH by design.
        check=True,
        capture_output=True,
    )


def test_capture_diff_includes_tracked_modification(tmp_path: Path) -> None:
    """A modified tracked file appears in the captured diff (real git, no mock).

    Replaces the former ``subprocess.run`` single-call mock test: the
    untracked-aware capture now issues multiple git calls, so the old
    one-call mock no longer reflects the contract. Real git keeps the
    test honest (see ``tests/unit/test_diff_capture.py``).
    """
    repo = tmp_path / "repo"
    _init_repo_with_tracked_file(repo)
    (repo / "a.txt").write_text("edited\n")

    diff = ca._capture_diff(repo)  # noqa: SLF001 — private capture under test.

    assert "a.txt" in diff
    assert "+edited" in diff


def test_capture_diff_includes_untracked_new_file(tmp_path: Path) -> None:
    """A NEW (untracked) file Codex creates must appear in the captured diff.

    Regression guard for the B-0010 reviewer-hallucination root cause: a
    plain ``git diff`` reports only tracked-file changes, so a created
    ``NOTES.md`` was invisible to the reviewer and to ``diff_nonempty``.
    Spec §8.9 counts "artifact changed (intended files touched)"; a new
    file is an intended touch, so the capture must include it.
    """
    repo = tmp_path / "repo"
    _init_repo_with_tracked_file(repo)
    (repo / "a.txt").write_text("edited\n")  # tracked modification
    (repo / "NOTES.md").write_text("# Notes\nbody\n")  # untracked Codex deliverable

    diff = ca._capture_diff(repo)  # noqa: SLF001 — private capture under test.

    assert "a.txt" in diff, "tracked modification must remain captured"
    assert "NOTES.md" in diff, "untracked new file must be captured"
    assert "# Notes" in diff, "new-file content must be captured"


# ---------------------------------------------------------------------------
# Observation 12561 - tempdir cleanup on seed/construction failure.
# ---------------------------------------------------------------------------


def _snapshot_codex_home_tempdirs() -> set[Path]:
    """Return the current set of jarvis-codex-home-* dirs in the tempdir."""
    import tempfile  # noqa: PLC0415

    return set(Path(tempfile.gettempdir()).glob(f"{ca._CODEX_HOME_PREFIX}*"))  # noqa: SLF001


def test_codex_action_cleans_tempdir_on_auth_copy_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shutil.copy2 raising during auth.json seed -> tempdir removed, error re-raised."""
    fake_home = tmp_path / "home"
    (fake_home / ".codex").mkdir(parents=True)
    (fake_home / ".codex" / "auth.json").write_text('{"OPENAI_API_KEY":"sk-test"}')
    monkeypatch.setenv("HOME", str(fake_home))

    def _raise_permission(*_a: object, **_k: object) -> None:
        msg = "denied"
        raise PermissionError(msg)

    monkeypatch.setattr("jarvis.execution.codex_action.shutil.copy2", _raise_permission)

    before = _snapshot_codex_home_tempdirs()
    with pytest.raises(PermissionError, match="denied"):
        ca.run_codex_action(task_goal="t", cwd=tmp_path, timeout_s=5.0)
    after = _snapshot_codex_home_tempdirs()

    assert after - before == set()


def test_codex_action_cleans_tempdir_on_write_text_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Path.write_text raising during AGENTS.md/config.toml seed -> tempdir removed."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    original_write_text = Path.write_text

    def _selective_raise(self: Path, *args: object, **kwargs: object) -> int:
        if self.name in ("AGENTS.md", "config.toml"):
            msg = "disk full"
            raise OSError(msg)
        return original_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", _selective_raise)

    before = _snapshot_codex_home_tempdirs()
    with pytest.raises(OSError, match="disk full"):
        ca.run_codex_action(task_goal="t", cwd=tmp_path, timeout_s=5.0)
    after = _snapshot_codex_home_tempdirs()

    assert after - before == set()


def test_codex_action_cleans_tempdir_on_client_init_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CodexAppServerClient(...) raising -> tempdir removed, original exc re-raised."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    class _RaisingClient:
        def __init__(self, **_kwargs: object) -> None:
            msg = "codex binary missing"
            raise FileNotFoundError(msg)

    monkeypatch.setattr(ca, "CodexAppServerClient", _RaisingClient)

    before = _snapshot_codex_home_tempdirs()
    with pytest.raises(FileNotFoundError, match="codex binary missing"):
        ca.run_codex_action(task_goal="t", cwd=tmp_path, timeout_s=5.0)
    after = _snapshot_codex_home_tempdirs()

    assert after - before == set()


# ---------------------------------------------------------------------------
# Empty-turn classification (dead-auth silent no-op, live-traced 2026-06-10).
# ---------------------------------------------------------------------------


def test_run_codex_action_zero_item_turn_classified_empty_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``turn/completed`` with zero streamed items → ``error="codex_empty_turn"``.

    Codex 0.130 with dead auth (rotated refresh token already consumed)
    completes the turn in ~2s with NO ``item/*`` notifications, zero
    tokens, and ``error=None`` — indistinguishable from a worker that ran
    and chose to do nothing. That silent shape folds into
    ``task.no_op + report_missing`` and Allen never hears about it,
    violating C5 (tool success alone is not goal evidence) and §3.4.11
    (error semantics must surface a Limitation). The driver must classify
    a zero-item turn as a failure so ``spawn_worker_handler`` 7b folds it
    into ``action.failed`` + Limitation Claim.
    """
    template = FakeClient(
        notifications=[
            {"method": "turn/completed", "params": {}},
        ],
    )
    _patch_client(monkeypatch, [template])
    _patch_diff_capture(monkeypatch)

    result = ca.run_codex_action(task_goal="t", cwd=tmp_path, timeout_s=5.0)

    assert result.error == "codex_empty_turn"
    assert result.interrupted is False


def test_run_codex_action_turn_with_items_not_classified_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any streamed ``item/*`` notification keeps the turn out of empty-turn.

    A live-but-lazy worker that never calls ``submit_report`` still emits
    at least item/started + an agent message; that stays on the
    ``report_missing`` ladder (J12 by-design) and must NOT be reclassified
    as ``codex_empty_turn``.
    """
    template = FakeClient(
        notifications=[
            {"method": "item/started", "params": {}},
            {
                "method": "item/completed",
                "params": {"item": {"type": "agentMessage", "text": "hi"}},
            },
            {"method": "turn/completed", "params": {}},
        ],
    )
    _patch_client(monkeypatch, [template])
    _patch_diff_capture(monkeypatch)

    result = ca.run_codex_action(task_goal="t", cwd=tmp_path, timeout_s=5.0)

    assert result.error is None
    assert result.submit_report is None


# ---------------------------------------------------------------------------
# Codex 0.130 token usage + nested turn id (cost 0/0 drift, live-traced
# 2026-06-10: turn/completed carries no usage field; cumulative usage
# arrives on thread/tokenUsage/updated; the turn id nests at turn.id).
# ---------------------------------------------------------------------------


def test_run_codex_action_reads_tokens_from_token_usage_updates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``thread/tokenUsage/updated`` totals flow into the result; last wins.

    Without this branch every healthy Codex 0.130 turn lands in the
    ``cost.recorded(kind=codex)`` ledger as 0/0 tokens and the turn id is
    ``None`` — the cost account is blind to Codex spend.
    """
    usage_first = {
        "threadId": "th-1",
        "turnId": "t-1",
        "tokenUsage": {
            "total": {
                "totalTokens": 13733,
                "inputTokens": 13149,
                "cachedInputTokens": 11136,
                "outputTokens": 584,
                "reasoningOutputTokens": 516,
            },
            "last": {"inputTokens": 13149, "outputTokens": 584},
            "modelContextWindow": 258400,
        },
    }
    usage_second = {
        "threadId": "th-1",
        "turnId": "t-1",
        "tokenUsage": {"total": {"inputTokens": 20011, "outputTokens": 902}},
    }
    completed = {
        "threadId": "th-1",
        "turn": {
            "id": "t-1",
            "items": [],
            "itemsView": "notLoaded",
            "status": "completed",
            "error": None,
        },
    }
    template = FakeClient(
        notifications=[
            {"method": "item/text", "params": {"text": "working"}},
            {"method": "thread/tokenUsage/updated", "params": usage_first},
            {"method": "thread/tokenUsage/updated", "params": usage_second},
            {"method": "turn/completed", "params": completed},
        ],
    )
    _patch_client(monkeypatch, [template])
    _patch_diff_capture(monkeypatch)

    result = ca.run_codex_action(task_goal="t", cwd=tmp_path, timeout_s=5.0)

    assert result.error is None
    # Cumulative totals — the last update wins.
    assert result.tokens_in == 20011
    assert result.tokens_out == 902
    # Codex 0.130 nests the id at params.turn.id on turn/completed.
    assert result.turn_id == "t-1"


def test_run_codex_action_legacy_flat_usage_takes_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy ``turn/completed.usage`` payload still wins over streamed totals."""
    template = FakeClient(
        notifications=[
            {"method": "item/text", "params": {"text": "working"}},
            {
                "method": "thread/tokenUsage/updated",
                "params": {"tokenUsage": {"total": {"inputTokens": 1, "outputTokens": 1}}},
            },
            {
                "method": "turn/completed",
                "params": {
                    "turnId": "legacy-1",
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                },
            },
        ],
    )
    _patch_client(monkeypatch, [template])
    _patch_diff_capture(monkeypatch)

    result = ca.run_codex_action(task_goal="t", cwd=tmp_path, timeout_s=5.0)

    assert result.tokens_in == 100
    assert result.tokens_out == 50
    assert result.turn_id == "legacy-1"


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"turn": {"id": "nested-1"}}, "nested-1"),
        ({"turnId": "flat-1"}, "flat-1"),
        ({"turn_id": "snake-1"}, "snake-1"),
        ({"turn": {"id": "nested-2"}, "turnId": "flat-2"}, "nested-2"),
        ({}, None),
        ({"turn": "not-a-mapping"}, None),
    ],
)
def test_extract_turn_id_handles_nested_and_flat(
    params: dict[str, Any],
    expected: str | None,
) -> None:
    """``_extract_turn_id`` prefers the 0.130 nested ``turn.id``, falls back flat."""
    assert ca._extract_turn_id(params) == expected  # noqa: SLF001 - test of private helper


# ---------------------------------------------------------------------------
# Rotated auth.json writeback (B-0004 follow-up, 2026-06-10).
#
# OpenAI refresh tokens are single-use rotating. B-0004 copy-seeds
# ~/.codex/auth.json into the throwaway isolated CODEX_HOME; when Codex
# refreshes inside it, the new token is written into the throwaway copy
# and destroyed on rmtree — canonical keeps the consumed predecessor and
# every later run dies with "refresh token was already used". The driver
# must write a rotated auth.json back before cleanup (ADR-0002 § Codex
# auth amendment).
# ---------------------------------------------------------------------------


def _write_auth_file(path: Path, *, last_refresh: str, marker: str) -> None:
    """Write a structurally-faithful fake codex auth.json."""
    import json  # noqa: PLC0415 - test-local helper import

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {
                    "id_token": "id",
                    "access_token": "at",
                    "refresh_token": marker,
                    "account_id": "acc",
                },
                "last_refresh": last_refresh,
            }
        )
    )


def test_sync_rotated_auth_writes_back_newer_token(tmp_path: Path) -> None:
    """Isolated auth.json with newer ``last_refresh`` replaces canonical, mode 0600."""
    home = tmp_path / "isolated-home"
    canonical = tmp_path / "dot-codex" / "auth.json"
    _write_auth_file(canonical, last_refresh="2026-06-10T10:00:00.000000Z", marker="OLD")
    _write_auth_file(
        home / "auth.json", last_refresh="2026-06-10T12:00:00.000000Z", marker="ROTATED"
    )

    ca._sync_rotated_auth(home, canonical)  # noqa: SLF001 - test of private helper

    assert "ROTATED" in canonical.read_text()
    assert (canonical.stat().st_mode & 0o777) == 0o600


def test_sync_rotated_auth_skips_when_last_refresh_unchanged(tmp_path: Path) -> None:
    """No refresh inside the isolated home → canonical untouched."""
    home = tmp_path / "isolated-home"
    canonical = tmp_path / "dot-codex" / "auth.json"
    same = "2026-06-10T10:00:00.000000Z"
    _write_auth_file(canonical, last_refresh=same, marker="CANONICAL")
    _write_auth_file(home / "auth.json", last_refresh=same, marker="ISOLATED-COPY")

    ca._sync_rotated_auth(home, canonical)  # noqa: SLF001

    assert "CANONICAL" in canonical.read_text()


def test_sync_rotated_auth_skips_when_canonical_newer(tmp_path: Path) -> None:
    """A concurrent external refresh (canonical newer) must never be clobbered."""
    home = tmp_path / "isolated-home"
    canonical = tmp_path / "dot-codex" / "auth.json"
    _write_auth_file(canonical, last_refresh="2026-06-10T12:00:00.000000Z", marker="NEWER")
    _write_auth_file(home / "auth.json", last_refresh="2026-06-10T10:00:00.000000Z", marker="STALE")

    ca._sync_rotated_auth(home, canonical)  # noqa: SLF001

    assert "NEWER" in canonical.read_text()


def test_sync_rotated_auth_restores_missing_canonical(tmp_path: Path) -> None:
    """Canonical absent (e.g. logged out mid-run) → isolated copy restores it."""
    home = tmp_path / "isolated-home"
    canonical = tmp_path / "dot-codex" / "auth.json"
    canonical.parent.mkdir(parents=True)
    _write_auth_file(home / "auth.json", last_refresh="2026-06-10T12:00:00.000000Z", marker="ONLY")

    ca._sync_rotated_auth(home, canonical)  # noqa: SLF001

    assert canonical.is_file()
    assert "ONLY" in canonical.read_text()


def test_sync_rotated_auth_tolerates_malformed_isolated(tmp_path: Path) -> None:
    """Garbage isolated auth.json → no raise, canonical untouched."""
    home = tmp_path / "isolated-home"
    home.mkdir()
    (home / "auth.json").write_text("{not json")
    canonical = tmp_path / "dot-codex" / "auth.json"
    _write_auth_file(canonical, last_refresh="2026-06-10T10:00:00.000000Z", marker="KEEP")

    ca._sync_rotated_auth(home, canonical)  # noqa: SLF001

    assert "KEEP" in canonical.read_text()


def test_sync_rotated_auth_tolerates_missing_isolated(tmp_path: Path) -> None:
    """No auth.json in the isolated home (seed skipped) → silent no-op."""
    home = tmp_path / "isolated-home"
    home.mkdir()
    canonical = tmp_path / "dot-codex" / "auth.json"
    _write_auth_file(canonical, last_refresh="2026-06-10T10:00:00.000000Z", marker="KEEP")

    ca._sync_rotated_auth(home, canonical)  # noqa: SLF001

    assert "KEEP" in canonical.read_text()


def test_sync_rotated_auth_skips_tokenless_isolated(tmp_path: Path) -> None:
    """A logged-out (token-less) isolated auth.json must not clobber canonical."""
    import json  # noqa: PLC0415 - test-local helper import

    home = tmp_path / "isolated-home"
    home.mkdir()
    (home / "auth.json").write_text(
        json.dumps({"tokens": None, "last_refresh": "2026-06-10T12:00:00.000000Z"})
    )
    canonical = tmp_path / "dot-codex" / "auth.json"
    _write_auth_file(canonical, last_refresh="2026-06-10T10:00:00.000000Z", marker="KEEP")

    ca._sync_rotated_auth(home, canonical)  # noqa: SLF001

    assert "KEEP" in canonical.read_text()


def test_run_codex_action_syncs_auth_before_home_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The driver invokes the writeback with the isolated home BEFORE rmtree."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    calls: list[tuple[Path, Path, bool]] = []

    def _recording_sync(isolated_home: Path, canonical_auth: Path) -> None:
        calls.append((isolated_home, canonical_auth, isolated_home.is_dir()))

    monkeypatch.setattr(ca, "_sync_rotated_auth", _recording_sync)

    template = FakeClient(
        notifications=[
            {"method": "item/text", "params": {"text": "working"}},
            {"method": "turn/completed", "params": {}},
        ],
    )
    _patch_client(monkeypatch, [template])
    _patch_diff_capture(monkeypatch)

    result = ca.run_codex_action(task_goal="t", cwd=tmp_path, timeout_s=5.0)

    assert result.error is None
    assert len(calls) == 1
    isolated_home, canonical_auth, existed_at_call_time = calls[0]
    assert ca._CODEX_HOME_PREFIX in str(isolated_home)  # noqa: SLF001
    assert canonical_auth == fake_home / ".codex" / "auth.json"
    # The dir must still exist when the sync runs — i.e. sync precedes rmtree.
    assert existed_at_call_time is True


@pytest.mark.parametrize(
    "total",
    [
        {"inputTokens": "abc", "outputTokens": 5},
        {"inputTokens": [1], "outputTokens": 5},
        {"inputTokens": 5, "outputTokens": None, "extra": 1},
    ],
)
def test_extract_token_usage_update_ignores_non_numeric(total: dict[str, Any]) -> None:
    """Malformed token counts must not raise out of the poll loop.

    An exception escaping ``run_codex_action``'s poll loop skips the
    ``_result`` finalizer entirely — the subprocess is never closed, the
    isolated CODEX_HOME leaks, and the rotated-auth sync never runs. A
    payload with non-numeric counts is dropped (``None``) instead.
    """
    params = {"tokenUsage": {"total": total}}
    result = ca._extract_token_usage_update(params)  # noqa: SLF001 - test of private helper
    assert result is None or isinstance(result[0], int)
