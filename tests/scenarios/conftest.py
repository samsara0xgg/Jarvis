"""Tier 2 scenario fixtures (real cloud LLM + real Codex subprocess).

Per ADR 0001 § Tier 2 invocation command + ADR 0002 Step 20 brief.

Responsibilities:

- (moved) ``pytest_addoption`` + ``pytest_collection_modifyitems`` now
  live in ``tests/conftest.py`` — the flags and the live-skip guarantee
  must hold for EVERY invocation scope under ``tests/``, and pytest only
  registers ``pytest_addoption`` from initial conftests (the ancestor
  path of the command-line args). Markers themselves are registered in
  ``pyproject.toml`` ``[tool.pytest.ini_options]``.

Tier-2 invocation
-----------------

Run the full Tier-2 J/K/L sweep on a Mac with a real OpenRouter key
and Codex CLI installed::

    uv run pytest tests/scenarios --live-codex --live-llm

Required environment (Step 20):

- ``codex`` CLI >= 0.125.0 on PATH (J1 preflight gate).
- ``OPENROUTER_PROXY_KEY`` env var set to a real key (> 20 chars,
  not a stub value — enforced by ``verify_api_key_present``).

Cost envelope (ADR 0002 Open Question 11): a single Tier-2 J-sweep
invocation costs approximately $4 - $15 in real Codex + OpenRouter
spend. The default CI path (no flags) costs $0.
- ``verify_api_key_present`` — autouse, ensures
  ``OPENROUTER_PROXY_KEY`` is set to a non-stub value before any
  scenario fixture instantiates a runtime / LLM client (acceptance G3).
- ``open_audit_hook`` — autouse, wraps ``builtins.open`` for the
  duration of every live scenario test and records the paths opened.
  Acceptance H7 Part B / G4 — asserts no path containing ``cassette``
  or ``recording`` is opened during scenario execution.
- ``seed_one_open_task`` — bootstraps a real :class:`JarvisRuntime`
  against ``tmp_path`` and seeds exactly one open task created 26h
  earlier (so "昨天那个 task" reads naturally to the LLM).
- ``write_llm_use_artifact`` — fixture that, after the scenario run,
  writes ``tests/_artifacts/llm_use_<ts>.json`` with the per-run token
  usage / finish_reason summary (acceptance G5).

Tier-2-only fixtures stay here; the option/skip hooks shared with the
rest of the suite live in ``tests/conftest.py`` (see above).
"""

from __future__ import annotations

import builtins
import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.runtime import bootstrap_runtime_app
from jarvis.state.event_log import emit_event

if TYPE_CHECKING:
    from collections.abc import Iterator

    from jarvis.runtime import JarvisRuntime

# Path roots --------------------------------------------------------------

_THIS_DIR: Path = Path(__file__).resolve().parent
_TESTS_DIR: Path = _THIS_DIR.parent
_ARTIFACTS_DIR: Path = _TESTS_DIR / "_artifacts"

# Forbidden values for OPENROUTER_PROXY_KEY (Acceptance G3 stub-block).
_STUB_API_KEY_VALUES: frozenset[str] = frozenset({"", "DUMMY", "test", "stub", "dummy"})
_MIN_API_KEY_LEN: int = 20

# Substrings whose presence in an opened path indicates recorded-LLM
# playback (Acceptance G4 / H7 Part B).
_FORBIDDEN_PATH_SUBSTRINGS: tuple[str, ...] = ("cassette", "recording")


# --- API key precondition -------------------------------------------------


@pytest.fixture(autouse=True)
def verify_api_key_present(request: pytest.FixtureRequest) -> None:
    """Fail fast if the scenario will be skipped or has no real API key.

    Only enforces when ``--live-llm`` is on AND the test is marked
    ``live_llm`` — otherwise this fixture is a no-op so non-scenario
    tests are not coupled to the env var.
    """
    if not request.config.getoption("--live-llm"):
        return
    if "live_llm" not in request.keywords:
        return
    raw = os.environ.get("OPENROUTER_PROXY_KEY", "")
    if raw in _STUB_API_KEY_VALUES or len(raw) <= _MIN_API_KEY_LEN:
        pytest.fail(
            "OPENROUTER_PROXY_KEY is missing or stub-valued; cannot run "
            "Tier 2 scenarios. Set the env var to a real OpenRouter proxy "
            f"key (>{_MIN_API_KEY_LEN} chars, not one of "
            f"{sorted(_STUB_API_KEY_VALUES)!r}).",
        )


# --- open() audit hook (H7 Part B / G4) -----------------------------------


@pytest.fixture(autouse=True)
def open_audit_hook(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[list[str]]:
    """Record every path that ``builtins.open`` is called on during a scenario.

    On teardown, asserts none of the recorded paths contains a
    forbidden substring (``cassette`` / ``recording``). Active only
    when the test is marked ``live_llm`` AND ``--live-llm`` is on; for
    everything else the fixture yields an empty list and does nothing.
    """
    if not request.config.getoption("--live-llm"):
        yield []
        return
    if "live_llm" not in request.keywords:
        yield []
        return

    opened: list[str] = []
    original_open = builtins.open

    def _audited_open(file: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        try:
            opened.append(str(file))
        except (TypeError, ValueError):  # pragma: no cover — pathological repr
            opened.append("<unrepr-able>")
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _audited_open)
    yield opened

    forbidden_hits = [
        path
        for path in opened
        if any(sub in path.lower() for sub in _FORBIDDEN_PATH_SUBSTRINGS)
    ]
    assert not forbidden_hits, (
        "H7 Part B / G4: scenario opened recorded-LLM-style paths:\n  "
        + "\n  ".join(forbidden_hits)
    )


# --- Seeded runtime -------------------------------------------------------


@pytest.fixture
def seed_one_open_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[JarvisRuntime]:
    """Bootstrap a real runtime against ``tmp_path`` and seed one open task.

    The seeded ``task.created`` event is timestamped 26h before now so
    that the LLM's natural reading of "昨天那个 task" maps to this row
    via the Resolver's single-open-task path.
    """
    monkeypatch.setenv("JARVIS_RUNTIME_ROOT", str(tmp_path))

    runtime = bootstrap_runtime_app(runtime_root=tmp_path)

    yesterday_ms = int(time.time() * 1000) - 26 * 3600 * 1000
    emit_event(
        runtime.conn,
        type="task.created",
        payload={
            "task_id": "task_X",
            "goal": "Implement Day-1 verify pipeline",
            "source": "manual",
        },
        ts_epoch_ms=yesterday_ms,
    )

    try:
        yield runtime
    finally:
        runtime.conn.close()


# --- Token-use artifact writer (G5) ---------------------------------------


def write_llm_use_artifact(
    runtime: JarvisRuntime,
    *,
    suffix: str = "",
) -> Path:
    """Write ``tests/_artifacts/llm_use[_<suffix>]_<ts>.json`` for the last LLM call.

    Acceptance G5 just requires the file exists with the expected
    keys; token counts are informational, not gated.

    Args:
        runtime: The :class:`JarvisRuntime` whose ``llm_client`` was
            just exercised.
        suffix: Optional filename suffix that distinguishes the
            happy-path and negative-case runs in the artifacts
            directory (e.g. ``"happy"`` / ``"fail"``).

    Returns:
        The path of the artifact written.
    """
    _ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    ts_epoch_ms = int(time.time() * 1000)
    payload: dict[str, Any] = {
        "model": runtime.llm_client.model,
        "input_tokens": runtime.llm_client.last_input_tokens,
        "output_tokens": runtime.llm_client.last_output_tokens,
        "finish_reason": runtime.llm_client.last_finish_reason,
        "ts_epoch_ms": ts_epoch_ms,
    }
    tag = f"_{suffix}" if suffix else ""
    artifact_path = _ARTIFACTS_DIR / f"llm_use{tag}_{ts_epoch_ms}.json"
    artifact_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return artifact_path
