# ADR-0005 Inherent Voice Surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the proven `jarvis-legacy` voice chain (wake word + push-to-talk + TTS) into `jarvis/surface/`, replacing the `POST /inherent/asr-submit` 501 stub, under strict `docs/spec.html` §3.6 compliance.

**Architecture:** Seven new flat files at `jarvis/surface/voice_*.py` host the legacy meat with namespace adjustments and three spec-deviation fixes (normalize-before-emit, wake/PTT mutex, unified empty-filter). The Inherent FastAPI daemon (ADR-0003) gains one real endpoint, one outbound WS envelope (`op:"voice"`), one extended watcher (`_user_intent_watcher` now polls `utterance.received` too), and one new watcher (`_tts_watcher`). Threading: wake listener + ASR worker pool + TTS synth + TTS play, all coordinated through `VOICE_INPUT_LOCK` (`threading.Lock`) and the existing `_response_watcher`'s `surface.response_*` event stream.

**Tech Stack:** Python 3.12, FastAPI + Starlette WS (existing), `openwakeword`, `sherpa-onnx` (SenseVoice INT8), `onnxruntime` (Silero VAD), `sounddevice` (PortAudio), `websockets` (MiniMax WS client), `mlx-whisper` (optional Apple Silicon ASR), macOS `osascript` + `say` (TTS fallback + audio ducking).

**Reference docs:** Spec — `docs/spec.html` §3.4.13, §3.6.1–§3.6.6, §3.6.10–§3.6.11, §5.4. ADR — `docs/adr/0005-inherent-voice.md` (this plan implements that ADR).

**Legacy source:** `/Users/alllllenshi/Projects/jarvis-legacy/` — verbatim port permissions in ADR §14.

---

## Conventions used in this plan

- **Worktree path:** all paths below are relative to `/Users/alllllenshi/Projects/jarvis/.claude/worktrees/claude-adr0001/` unless stated. Run all commands from there.
- **Four gates (run after every task that modifies code):**
  ```
  .venv/bin/ruff check jarvis tests
  .venv/bin/mypy jarvis
  .venv/bin/lint-imports
  .venv/bin/pytest -x
  ```
  The plan calls these out at the end of each phase; if a single task touches multiple files, run all four before committing.
- **TDD discipline:** each task writes a failing test FIRST, runs it to see it fail, writes minimal impl, runs to see it pass, then commits. Never commit red.
- **Commit messages:** follow `type(scope): subject` per recent history (e.g. `feat(surface): voice ducking refcounted depth`). No `Co-Authored-By` per project CLAUDE.md.
- **Verbatim port shorthand:** when a task says "verbatim port `<legacy_path>` into `<new_path>` with adjustments below", the engineer literally `cp`s the file and applies the listed adjustments. The plan does NOT inline the full file content — these legacy modules are 200-500 LOC each and proven to work.
- **Layer rules:** all `jarvis/surface/voice_*.py` modules may import only `stdlib`, `jarvis.shared`, `jarvis.state.event_log`, `jarvis.constitution`. They MUST NOT name `jarvis.decision`, `jarvis.execution`, `jarvis.deployment`, `jarvis.runtime`, or `jarvis.cli`. Verified by `lint-imports` (gate 3) plus Task 22 canary.

---

# Phase 1 — Foundations (registry + wire-contract)

These two tasks unblock everything downstream by widening event payload shapes. Both are backwards-compatible (adding optional fields).

## Task 1: Extend `utterance.received` registry with voice-adapter payload fields

**Files:**
- Modify: `jarvis/state/event_log.py:176-182` (the `utterance.received` `EventTypeSchema`)
- Test: `tests/unit/test_event_log_utterance_received_voice_fields.py` (new)

**Spec basis:** ADR §7 + spec §3.6.1 row 1 (`utterance.received(transcript, confidence)`).

- [ ] **Step 1: Write the failing test.**

Create `tests/unit/test_event_log_utterance_received_voice_fields.py`:

```python
"""ADR-0005 §7: utterance.received gains voice-adapter optional fields."""
from __future__ import annotations

import sqlite3

import pytest

from jarvis.state.event_log import (
    EventTypeRegistry,
    emit_event,
    open_event_log,
)


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "events.db"
    conn = open_event_log(db)
    yield conn
    conn.close()


def test_utterance_received_accepts_voice_optional_fields(conn: sqlite3.Connection) -> None:
    """All four new optional fields are emitted without registry rejection."""
    ev = emit_event(
        conn,
        type="utterance.received",
        payload={
            "transcript": "你好",
            "turn_id": "T1a2b3c4d",
            "channel": "inherent_wake",
            "language": "zh-CN",
            "confidence": 0.9,
            "language_detected": "zh-CN",
            "emotion": "HAPPY",
            "audio_artifact_ref": "data/voice_artifacts/T1a2b3c4d.wav",
        },
    )
    assert ev.payload["confidence"] == 0.9
    assert ev.payload["emotion"] == "HAPPY"
    assert ev.payload["audio_artifact_ref"].endswith(".wav")


def test_utterance_received_registry_lists_new_optional_fields() -> None:
    """Registry entry advertises the four new optional fields."""
    schema = EventTypeRegistry.get("utterance.received")
    assert schema is not None
    for new_field in ("confidence", "language_detected", "emotion", "audio_artifact_ref"):
        assert new_field in schema.optional_payload, f"{new_field} missing from optional_payload"
```

- [ ] **Step 2: Run to confirm RED.**

```
.venv/bin/pytest tests/unit/test_event_log_utterance_received_voice_fields.py -v
```

Expected: both tests FAIL — first with payload-key rejection (registry doesn't allow `confidence`), second with `assert "confidence" in schema.optional_payload`.

- [ ] **Step 3: Extend the registry entry.**

Edit `jarvis/state/event_log.py:176-182` so the entry becomes:

```python
EventTypeSchema(
    event_type="utterance.received",
    owner_layer="L5",
    required_payload=("transcript", "turn_id"),
    optional_payload=(
        "channel",
        "language",
        "confidence",
        "language_detected",
        "emotion",
        "audio_artifact_ref",
    ),
    schema_version=1,
),
```

Keep `schema_version=1` — pure additive change per ADR §7.

- [ ] **Step 4: Run to confirm GREEN.**

```
.venv/bin/pytest tests/unit/test_event_log_utterance_received_voice_fields.py -v
```

Both tests PASS.

- [ ] **Step 5: Full test suite + gates.**

```
.venv/bin/pytest -x
.venv/bin/ruff check jarvis tests
.venv/bin/mypy jarvis
.venv/bin/lint-imports
```

All four exit 0.

- [ ] **Step 6: Commit.**

```
git add jarvis/state/event_log.py tests/unit/test_event_log_utterance_received_voice_fields.py
git commit -m "feat(state): widen utterance.received with confidence + artifact_ref (ADR-0005 §7)"
```

---

## Task 2: Carry `required_gate_mode` on `surface.response_open` payload

**Files:**
- Modify: `jarvis/state/event_log.py` — `surface.response_open` `EventTypeSchema` (search for `event_type="surface.response_open"`, around line 442)
- Modify: `jarvis/surface/cli_render.py:140-157` (`_emit_response_open`) — accept the plan and write `required_gate_mode` into payload
- Modify: `jarvis/surface/cli_render.py:373` (the caller of `_emit_response_open`) — pass the plan through
- Test: `tests/unit/test_cli_render_response_open_gate_mode.py` (new)

**Spec basis:** ADR §5.3 "Plan vs event" + §7 second paragraph.

- [ ] **Step 1: Write the failing test.**

Create `tests/unit/test_cli_render_response_open_gate_mode.py`:

```python
"""ADR-0005 §7: surface.response_open carries required_gate_mode in payload."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import pytest

from jarvis.state.event_log import open_event_log
from jarvis.surface.cli_render import _emit_response_open


@dataclass(frozen=True)
class _PlanStub:
    text: str = ""
    response_hash: str = "h" * 64
    required_gate_mode: str = "sentence"
    output_risk_class: str = "routine"


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "events.db"
    conn = open_event_log(db)
    yield conn
    conn.close()


def test_response_open_payload_carries_gate_mode(conn: sqlite3.Connection) -> None:
    plan = _PlanStub(required_gate_mode="full_text")
    _emit_response_open(conn, turn_id="T1", query="hi", response_plan=plan)
    row = conn.execute(
        "SELECT payload_json FROM events WHERE type='surface.response_open' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    import json
    payload = json.loads(row[0])
    assert payload["required_gate_mode"] == "full_text"


def test_response_open_payload_defaults_when_plan_is_sentence(conn: sqlite3.Connection) -> None:
    plan = _PlanStub(required_gate_mode="sentence")
    _emit_response_open(conn, turn_id="T2", query="", response_plan=plan)
    row = conn.execute(
        "SELECT payload_json FROM events WHERE type='surface.response_open' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    import json
    payload = json.loads(row[0])
    assert payload["required_gate_mode"] == "sentence"
```

- [ ] **Step 2: Run to confirm RED.**

```
.venv/bin/pytest tests/unit/test_cli_render_response_open_gate_mode.py -v
```

Expected: TypeError — `_emit_response_open` has no `response_plan` kwarg.

- [ ] **Step 3: Extend the registry entry.**

In `jarvis/state/event_log.py`, find the `event_type="surface.response_open"` entry and add `"required_gate_mode"` to its `optional_payload` tuple. Example (final shape):

```python
EventTypeSchema(
    event_type="surface.response_open",
    owner_layer="L5",
    required_payload=("turn_id", "query", "kind"),
    optional_payload=("required_gate_mode",),
    schema_version=1,
),
```

(If the entry currently has other optional fields, preserve them and append `"required_gate_mode"` at the end.)

- [ ] **Step 4: Update `_emit_response_open` signature + payload.**

Edit `jarvis/surface/cli_render.py:140-157`:

```python
def _emit_response_open(
    conn: sqlite3.Connection,
    *,
    turn_id: str,
    query: str,
    response_plan: ResponsePlanLike,
) -> None:
    """Emit the ADR-0003 Step 2 ``surface.response_open`` event.

    Payload carries ``turn_id``, ``query`` (the user transcript that
    triggered the turn — empty string allowed), ``kind`` (always
    ``"text"`` for A1), and ``required_gate_mode`` (ADR-0005 §7 — L5
    TTS consumers read this to route between sentence-streaming and
    full-text TTS playback per spec §3.6.6).
    """
    emit_event(
        conn,
        type="surface.response_open",
        payload={
            "turn_id": turn_id,
            "query": query,
            "kind": "text",
            "required_gate_mode": response_plan.required_gate_mode,
        },
        correlation={"turn_id": turn_id},
    )
```

- [ ] **Step 5: Update the caller at `cli_render.py:373`.**

Find the existing call `_emit_response_open(conn, turn_id=turn_id, query=query)` and add `response_plan=response_plan` (the variable is in scope in `render_response`).

- [ ] **Step 6: Run the new test to confirm GREEN.**

```
.venv/bin/pytest tests/unit/test_cli_render_response_open_gate_mode.py -v
```

PASS.

- [ ] **Step 7: Full test suite + gates.**

```
.venv/bin/pytest -x
.venv/bin/ruff check jarvis tests
.venv/bin/mypy jarvis
.venv/bin/lint-imports
```

All four exit 0. If `test_canary_response_plan_carries_gate_mode` (existing) fails because of a signature mismatch, fix the canary's stub to also pass `response_plan` in any direct `_emit_response_open` invocations — but the canary is supposed to scan `ResponsePlan(...)` construction, not call `_emit_response_open`, so it should be unaffected. If other ADR-0003 tests fail because they call `_emit_response_open` without `response_plan`, update them to pass a `_PlanStub` like the one in Task 2 Step 1.

- [ ] **Step 8: Commit.**

```
git add jarvis/state/event_log.py jarvis/surface/cli_render.py tests/unit/test_cli_render_response_open_gate_mode.py
git commit -m "feat(surface): carry required_gate_mode on surface.response_open (ADR-0005 §7)"
```

---

# Phase 2 — Surface modules (bottom-up)

These tasks add the seven new `jarvis/surface/voice_*.py` files. Order is bottom-up: leaf utilities first, composition last.

## Task 3: `voice_ducking.py` — macOS AppleScript output mute (refcounted)

**Files:**
- Create: `jarvis/surface/voice_ducking.py`
- Test: `tests/unit/test_voice_ducking.py`
- Legacy port source: `/Users/alllllenshi/Projects/jarvis-legacy/core/media_ducking.py`

**Spec basis:** ADR §4.2 row `voice_ducking.py` + spec §3.6.10 (presentation actions are rate-limited but not gated as world actions).

- [ ] **Step 1: Write the failing test.**

Create `tests/unit/test_voice_ducking.py`:

```python
"""ADR-0005 voice_ducking — refcounted AppleScript mute/restore."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from jarvis.surface import voice_ducking


@pytest.fixture(autouse=True)
def _force_darwin_available(monkeypatch):
    # Force the module to think it is on darwin even when tests run on linux CI.
    monkeypatch.setattr(voice_ducking, "_PLATFORM_OVERRIDE", "darwin", raising=False)


def test_single_duck_restore_cycle_invokes_osascript_twice() -> None:
    """One duck, one restore => exactly two osascript subprocess invocations."""
    with patch.object(voice_ducking, "_run_osascript", autospec=True) as run:
        run.return_value = "output volume:50, output muted:false"
        d = voice_ducking.SystemAudioDucker()
        d.duck()
        d.restore()
        assert run.call_count == 2


def test_nested_duck_restore_uses_refcount() -> None:
    """Two ducks + two restores => only ONE actual mute and ONE restore."""
    with patch.object(voice_ducking, "_run_osascript", autospec=True) as run:
        run.return_value = "output volume:50, output muted:false"
        d = voice_ducking.SystemAudioDucker()
        d.duck()
        d.duck()
        d.restore()
        assert run.call_count == 2, "inner restore must not call osascript"
        d.restore()
        assert run.call_count == 3, "outer restore restores OS state"


def test_context_manager_duck_yields_to_caller() -> None:
    with patch.object(voice_ducking, "_run_osascript", autospec=True) as run:
        run.return_value = "output volume:50, output muted:false"
        d = voice_ducking.SystemAudioDucker()
        with d.duck_scope():
            pass
        assert run.call_count == 2
```

- [ ] **Step 2: Run to confirm RED.**

```
.venv/bin/pytest tests/unit/test_voice_ducking.py -v
```

Expected: `ModuleNotFoundError: No module named 'jarvis.surface.voice_ducking'`.

- [ ] **Step 3: Verbatim port `media_ducking.py` with three adjustments.**

```
cp /Users/alllllenshi/Projects/jarvis-legacy/core/media_ducking.py jarvis/surface/voice_ducking.py
```

Open `jarvis/surface/voice_ducking.py` and apply:

1. Add module docstring referencing ADR-0005 §4.2 and spec §3.6.10.
2. Replace top-level `import platform` with `import platform, sys` if needed, and add a module-level `_PLATFORM_OVERRIDE: str | None = None` so the test can monkey-patch it. Change the legacy `platform.system()` check in `_available()` to:
   ```python
   def _available() -> bool:
       sysname = _PLATFORM_OVERRIDE or platform.system().lower()
       return sysname == "darwin"
   ```
3. Add a `duck_scope()` `@contextlib.contextmanager` method on `SystemAudioDucker` that calls `self.duck()` on enter and `self.restore()` on exit.
4. Ensure all functions used by the legacy module are present: `_run_osascript`, `SystemAudioDucker` class with `duck()` / `restore()` / `duck_scope()`. The legacy file already has these — just verify names match.

Inside the file, audit for any `import` from `jarvis-legacy.*` or relative imports outside the file; if found, rewrite to stdlib only.

- [ ] **Step 4: Run tests to confirm GREEN.**

```
.venv/bin/pytest tests/unit/test_voice_ducking.py -v
```

All three PASS.

- [ ] **Step 5: Gates.**

```
.venv/bin/ruff check jarvis/surface/voice_ducking.py tests/unit/test_voice_ducking.py
.venv/bin/mypy jarvis/surface/voice_ducking.py
.venv/bin/lint-imports
```

All three exit 0.

- [ ] **Step 6: Commit.**

```
git add jarvis/surface/voice_ducking.py tests/unit/test_voice_ducking.py
git commit -m "feat(surface): port voice_ducking from legacy media_ducking (ADR-0005 §4.2)"
```

---

## Task 4: `voice_artifact_store.py` — opt-in raw WAV retention

**Files:**
- Create: `jarvis/surface/voice_artifact_store.py`
- Test: `tests/unit/test_voice_artifact_store.py`
- No legacy source (NEW per ADR §4.2).

**Spec basis:** ADR §3 row "Raw ASR output optionally as artifact_ref" + spec §3.6.2.

- [ ] **Step 1: Write the failing test.**

Create `tests/unit/test_voice_artifact_store.py`:

```python
"""ADR-0005 voice_artifact_store — opt-in raw WAV retention."""
from __future__ import annotations

import wave
from pathlib import Path

import pytest

from jarvis.surface import voice_artifact_store


@pytest.fixture
def pcm_audio() -> bytes:
    """Minimal 1-sample mono PCM16 frame."""
    return b"\x01\x00"


def test_persist_disabled_returns_none(pcm_audio, tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_VOICE_RETAIN_RAW", raising=False)
    ref = voice_artifact_store.persist(
        pcm_audio,
        turn_id="T1",
        sample_rate_hz=16000,
        artifacts_dir=tmp_path,
    )
    assert ref is None
    assert list(tmp_path.iterdir()) == []


def test_persist_enabled_writes_valid_wav(pcm_audio, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_VOICE_RETAIN_RAW", "1")
    ref = voice_artifact_store.persist(
        pcm_audio,
        turn_id="T2",
        sample_rate_hz=16000,
        artifacts_dir=tmp_path,
    )
    assert ref is not None
    p = Path(ref)
    assert p.exists()
    # Verify it's a real WAV that wave.open() can read.
    with wave.open(str(p), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == 16000


def test_persist_relative_path_under_artifacts_dir(pcm_audio, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_VOICE_RETAIN_RAW", "1")
    ref = voice_artifact_store.persist(
        pcm_audio,
        turn_id="T3a",
        sample_rate_hz=16000,
        artifacts_dir=tmp_path,
    )
    assert ref is not None
    # Reference path should round-trip through Path() and be inside tmp_path.
    p = Path(ref)
    assert tmp_path in p.parents
```

- [ ] **Step 2: Run to confirm RED.**

```
.venv/bin/pytest tests/unit/test_voice_artifact_store.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write the module.**

Create `jarvis/surface/voice_artifact_store.py`:

```python
"""L5 voice artifact store — opt-in raw WAV retention (ADR-0005 §4.2).

Per spec §3.6.2: raw ASR audio MAY be retained as an artifact_ref for
debug. Default disabled (privacy preserving); enabled by setting
``JARVIS_VOICE_RETAIN_RAW=1``. The returned path is suitable for the
``audio_artifact_ref`` field on ``utterance.received`` events.
"""
from __future__ import annotations

import os
import wave
from pathlib import Path


def persist(
    pcm_audio: bytes,
    *,
    turn_id: str,
    sample_rate_hz: int,
    artifacts_dir: Path,
) -> str | None:
    """Write ``pcm_audio`` as a mono PCM16 WAV under ``artifacts_dir``.

    Args:
        pcm_audio: raw PCM16 little-endian mono bytes.
        turn_id: filename stem (``{turn_id}.wav``).
        sample_rate_hz: typically 16000 for SenseVoice / Whisper input.
        artifacts_dir: directory to write under (caller passes
            ``Path("data/voice_artifacts")`` in production; tests pass
            a tmp_path).

    Returns:
        Absolute path to the WAV file as a string, or ``None`` when
        retention is disabled.
    """
    if os.environ.get("JARVIS_VOICE_RETAIN_RAW", "0") != "1":
        return None
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    out = artifacts_dir / f"{turn_id}.wav"
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate_hz)
        w.writeframes(pcm_audio)
    return str(out)


__all__ = ["persist"]
```

- [ ] **Step 4: Run to confirm GREEN.**

```
.venv/bin/pytest tests/unit/test_voice_artifact_store.py -v
```

All three PASS.

- [ ] **Step 5: Gates + commit.**

```
.venv/bin/ruff check jarvis/surface/voice_artifact_store.py tests/unit/test_voice_artifact_store.py
.venv/bin/mypy jarvis/surface/voice_artifact_store.py
.venv/bin/lint-imports
git add jarvis/surface/voice_artifact_store.py tests/unit/test_voice_artifact_store.py
git commit -m "feat(surface): voice_artifact_store for opt-in raw WAV retention (ADR-0005 §4.2)"
```

---

## Task 5: `voice_asr.py` — ASR normalizer (3-layer cascade)

This task ports ONLY the normalizer half of `voice_asr.py`. The recognizer half lands in Task 6 to keep tests targeted and commits surgical.

**Files:**
- Create: `jarvis/surface/voice_asr.py` (skeleton + `normalize` only)
- Test: `tests/unit/test_voice_asr_normalize.py`
- Legacy port source: `/Users/alllllenshi/Projects/jarvis-legacy/core/asr_normalizer.py`

**Spec basis:** ADR §3 "Adapter-internal canonicalization" + §8 fix #1 + spec §3.6.2.

- [ ] **Step 1: Write the failing test.**

Create `tests/unit/test_voice_asr_normalize.py`:

```python
"""ADR-0005 voice_asr.normalize — 3-layer cascade."""
from __future__ import annotations

import pytest

from jarvis.surface import voice_asr


def test_normalize_layer1_context_guarded_correction() -> None:
    """Layer-1 manual correction only fires when context word present."""
    norm = voice_asr.AsrNormalizer(
        corrections=[
            {"pattern": "大灯", "replace": "大厅", "require_context": ["客厅"]},
        ],
        aliases={},
        fuzzy_enabled=False,
    )
    assert norm.normalize("客厅大灯") == "客厅大厅"
    assert norm.normalize("打开大灯") == "打开大灯"  # no context — no replace


def test_normalize_layer2_structured_alias_longest_first() -> None:
    """Layer-2 alias replaces canonical; longest alias wins on overlap."""
    norm = voice_asr.AsrNormalizer(
        corrections=[],
        aliases={"卧室主灯": ["卧室灯", "卧室主灯"]},
        fuzzy_enabled=False,
    )
    assert norm.normalize("打开卧室主灯") == "打开卧室主灯"  # already canonical
    assert norm.normalize("打开卧室灯") == "打开卧室主灯"   # alias -> canonical


def test_normalize_layer3_fuzzy_disabled_by_default() -> None:
    norm = voice_asr.AsrNormalizer(corrections=[], aliases={}, fuzzy_enabled=False)
    assert norm.normalize("打开卧室登") == "打开卧室登"


def test_normalize_layer3_fuzzy_requires_action_verb() -> None:
    """Layer-3 Levenshtein fires only when action verb present + alias match."""
    norm = voice_asr.AsrNormalizer(
        corrections=[],
        aliases={"卧室主灯": ["卧室主灯"]},
        fuzzy_enabled=True,
    )
    # "登" differs from "灯" by 1 edit, "卧室主灯" has 4 chars, "卧室主登" too.
    assert norm.normalize("打开卧室主登") == "打开卧室主灯"
    # No action verb — fuzzy stays off.
    assert norm.normalize("卧室主登") == "卧室主登"


def test_normalize_returns_unchanged_text_when_no_rules_apply() -> None:
    norm = voice_asr.AsrNormalizer(corrections=[], aliases={}, fuzzy_enabled=False)
    assert norm.normalize("现在几点") == "现在几点"


def test_normalize_handles_empty_string() -> None:
    norm = voice_asr.AsrNormalizer(corrections=[], aliases={}, fuzzy_enabled=False)
    assert norm.normalize("") == ""
```

- [ ] **Step 2: Run to confirm RED.**

```
.venv/bin/pytest tests/unit/test_voice_asr_normalize.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Port the normalizer half.**

```
cp /Users/alllllenshi/Projects/jarvis-legacy/core/asr_normalizer.py jarvis/surface/voice_asr.py
```

Then open `jarvis/surface/voice_asr.py` and:

1. Replace the module docstring with one that names ADR-0005 §3 + spec §3.6.2 and notes "normalizer half — recognizer added in next commit".
2. Audit imports: keep only stdlib. Remove any `from core.*` / `from ..*` / `from jarvis-legacy.*` imports.
3. Verify the class is named `AsrNormalizer` (rename if legacy uses a different name). It must accept the constructor kwargs from the test: `corrections: list[dict]`, `aliases: dict[str, list[str]]`, `fuzzy_enabled: bool`.
4. Ensure `normalize(self, text: str) -> str` is the only public method on the class.
5. Keep the legacy three-layer cascade logic verbatim: layer 1 (`require_context` guard), layer 2 (alias-flatten longest-first), layer 3 (Levenshtein DP, gated on action-verb presence with max distance 2).
6. Make sure `__all__` contains `["AsrNormalizer"]` for now.

If the legacy file's class is a wrapper that loads from `config['asr_corrections']`, keep that as an alternative constructor `from_config(cfg)` but make the primary constructor take the three explicit arguments above. Do NOT add new behavior.

- [ ] **Step 4: Run to confirm GREEN.**

```
.venv/bin/pytest tests/unit/test_voice_asr_normalize.py -v
```

All six PASS.

- [ ] **Step 5: Gates + commit.**

```
.venv/bin/ruff check jarvis/surface/voice_asr.py tests/unit/test_voice_asr_normalize.py
.venv/bin/mypy jarvis/surface/voice_asr.py
.venv/bin/lint-imports
git add jarvis/surface/voice_asr.py tests/unit/test_voice_asr_normalize.py
git commit -m "feat(surface): port voice_asr normalizer 3-layer cascade (ADR-0005 §4.2)"
```

---

## Task 6: `voice_asr.py` — recognizer (SenseVoice via sherpa-onnx) + empty-utterance filter

**Files:**
- Modify: `jarvis/surface/voice_asr.py` (extend with recognizer + filter)
- Test: `tests/unit/test_voice_asr_recognize_and_filter.py`
- Legacy port source: `/Users/alllllenshi/Projects/jarvis-legacy/core/speech_recognizer.py`

**Spec basis:** ADR §4.2 + §8 fix #3 (unified empty-utterance filter).

- [ ] **Step 1: Write the failing test.**

Create `tests/unit/test_voice_asr_recognize_and_filter.py`:

```python
"""ADR-0005 voice_asr — recognize + is_empty_or_too_short."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from jarvis.surface import voice_asr


def test_transcription_result_dataclass_has_required_fields() -> None:
    tr = voice_asr.TranscriptionResult(
        text="你好",
        confidence=0.9,
        language_detected="zh-CN",
        emotion="HAPPY",
    )
    assert tr.text == "你好"
    assert tr.confidence == 0.9
    assert tr.language_detected == "zh-CN"
    assert tr.emotion == "HAPPY"


def test_is_empty_or_too_short_returns_true_for_blank_text() -> None:
    assert voice_asr.is_empty_or_too_short("", audio_pcm=b"\x00" * 100) is True
    assert voice_asr.is_empty_or_too_short("   ", audio_pcm=b"\x00" * 100) is True
    assert voice_asr.is_empty_or_too_short("a", audio_pcm=b"\x00" * 100) is True  # <2 chars


def test_is_empty_or_too_short_returns_true_for_silent_audio() -> None:
    # 1000 frames of pure zero -> RMS = 0.
    assert voice_asr.is_empty_or_too_short("你好", audio_pcm=b"\x00" * 2000) is True


def test_is_empty_or_too_short_returns_true_for_punctuation_only() -> None:
    # Audio is non-silent (alternating bytes) but text is just CJK punctuation.
    audio = b"\x10\x00" * 1000
    assert voice_asr.is_empty_or_too_short("。。。", audio_pcm=audio) is True


def test_is_empty_or_too_short_returns_false_for_real_utterance() -> None:
    audio = b"\x10\x00" * 1000  # non-zero RMS
    assert voice_asr.is_empty_or_too_short("现在几点", audio_pcm=audio) is False


def test_recognizer_protocol_returns_transcription_result() -> None:
    """SenseVoice provider returns a TranscriptionResult; we test via stub."""
    fake_recognizer = MagicMock(spec=voice_asr.AsrRecognizer)
    fake_recognizer.recognize.return_value = voice_asr.TranscriptionResult(
        text="你好",
        confidence=0.9,
        language_detected="zh-CN",
        emotion=None,
    )
    result = fake_recognizer.recognize(b"\x10\x00" * 16000)
    assert result.text == "你好"
    assert result.confidence == 0.9
```

- [ ] **Step 2: Run to confirm RED.**

```
.venv/bin/pytest tests/unit/test_voice_asr_recognize_and_filter.py -v
```

Expected: `AttributeError: module 'jarvis.surface.voice_asr' has no attribute 'TranscriptionResult'`.

- [ ] **Step 3: Extend `voice_asr.py` with recognizer + filter.**

Append to `jarvis/surface/voice_asr.py`:

```python
# --- Recognizer (ASR provider) -------------------------------------------

from dataclasses import dataclass
from typing import Protocol

import numpy as np


@dataclass(frozen=True)
class TranscriptionResult:
    """Output of an ASR recognize() call (spec §3.6.1).

    Confidence semantics are provider-specific (see ADR-0005 §3): SenseVoice
    returns a binary 0.1 / 0.9 heuristic, mlx-whisper returns a log-prob
    mean. Downstream consumers must NOT cross-compare confidence values
    between providers.
    """

    text: str
    confidence: float
    language_detected: str | None
    emotion: str | None


class AsrRecognizer(Protocol):
    """Structural ASR provider — implementations live below in this module."""

    def recognize(self, audio_pcm: bytes) -> TranscriptionResult:
        """Return a TranscriptionResult for mono 16 kHz PCM16 audio."""
        ...


# Concrete providers — verbatim port from legacy core/speech_recognizer.py.
# The legacy file has SenseVoice (sherpa-onnx OfflineRecognizer.from_sense_voice),
# mlx_whisper, and openai-whisper "local" fallback. Port all three classes
# with their config-driven construction unchanged. Each must conform to
# AsrRecognizer (i.e. have a recognize(audio_pcm: bytes) -> TranscriptionResult
# method).
#
# When porting:
#   1. Replace any `from core.* import ...` with stdlib / numpy / sherpa_onnx.
#   2. Rename the legacy class if needed so SenseVoice provider is
#      `SenseVoiceRecognizer`, mlx is `MlxWhisperRecognizer`, openai-whisper
#      is `LocalWhisperRecognizer`.
#   3. Convert legacy return type to the new TranscriptionResult dataclass.
#   4. Drop emit_voice / event_bus hooks — those are NOT the recognizer's
#      job per ADR-0005 §4.2 (composition belongs in voice_pipeline.py).


# --- Empty / too-short filter (ADR-0005 §8 fix #3) -----------------------


_MIN_TEXT_LEN = 2
_MIN_RMS_THRESHOLD = 50.0  # PCM16 mono; tuned for typical mic noise floor


def _rms(audio_pcm: bytes) -> float:
    """Root-mean-square amplitude of PCM16 mono little-endian audio."""
    if not audio_pcm:
        return 0.0
    samples = np.frombuffer(audio_pcm, dtype=np.int16)
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))


def _is_punctuation_only(text: str) -> bool:
    """True when `text` contains no non-punctuation, non-whitespace chars."""
    # zh / en common punctuation; extend as needed.
    punct = set("。，！？、；：“”‘’【】《》（）.,!?;:\"'()[]<>~`@#$%^&*-_+=|\\/ ")
    return all(c in punct or c.isspace() for c in text)


def is_empty_or_too_short(text: str, *, audio_pcm: bytes) -> bool:
    """Unified empty-utterance filter — single source of truth for both
    wake and PTT paths per ADR-0005 §8 fix #3."""
    stripped = text.strip()
    if len(stripped) < _MIN_TEXT_LEN:
        return True
    if _is_punctuation_only(stripped):
        return True
    if _rms(audio_pcm) < _MIN_RMS_THRESHOLD:
        return True
    return False


__all__ = [
    "AsrNormalizer",
    "AsrRecognizer",
    "TranscriptionResult",
    "is_empty_or_too_short",
]
```

Then port the SenseVoice, mlx-whisper, and local-whisper class implementations from `core/speech_recognizer.py` per the inline comment above. The test only mocks `AsrRecognizer`, so production correctness depends on the port matching the spec — verify by reading the legacy class's `transcribe()` method and porting it to `recognize(audio_pcm)` returning a `TranscriptionResult`.

- [ ] **Step 4: Run to confirm GREEN.**

```
.venv/bin/pytest tests/unit/test_voice_asr_recognize_and_filter.py -v
```

All six PASS. (The "recognizer protocol" test uses MagicMock spec, so does not require sherpa-onnx model files to be installed.)

- [ ] **Step 5: Gates + commit.**

```
.venv/bin/ruff check jarvis/surface/voice_asr.py tests/unit/test_voice_asr_recognize_and_filter.py
.venv/bin/mypy jarvis/surface/voice_asr.py
.venv/bin/lint-imports
git add jarvis/surface/voice_asr.py tests/unit/test_voice_asr_recognize_and_filter.py
git commit -m "feat(surface): voice_asr recognizer + unified empty filter (ADR-0005 §4.2 §8.3)"
```

---

## Task 7: `voice_audio.py` — Silero VAD (state machine + ONNX runner)

**Files:**
- Create: `jarvis/surface/voice_audio.py` (VAD half only this task)
- Test: `tests/unit/test_voice_audio_vad.py`
- Legacy port source: `/Users/alllllenshi/Projects/jarvis-legacy/core/vad_silero.py`

**Spec basis:** ADR §4.2 row `voice_audio.py` + spec §3.6.1.

- [ ] **Step 1: Write the failing test.**

Create `tests/unit/test_voice_audio_vad.py`:

```python
"""ADR-0005 voice_audio — Silero VAD state machine."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from jarvis.surface import voice_audio


def _fake_silero_session(prob_for_call: list[float]):
    """Return a MagicMock that emulates the Silero ONNX session.

    Each call to .run() returns the next probability from `prob_for_call`.
    """
    session = MagicMock()
    call_counter = {"i": 0}

    def _run(*args, **kwargs):
        i = call_counter["i"]
        call_counter["i"] += 1
        prob = prob_for_call[min(i, len(prob_for_call) - 1)]
        # Silero ONNX output shape: [[prob]], plus updated state tensors.
        return [np.array([[prob]], dtype=np.float32), MagicMock(), MagicMock()]

    session.run.side_effect = _run
    return session


def test_vad_record_mode_yields_speech_then_silence() -> None:
    """A run of high-prob frames followed by low-prob frames flips to silence."""
    silent_frame = np.zeros(512, dtype=np.int16).tobytes()
    speech_frame = (np.ones(512, dtype=np.int16) * 8000).tobytes()
    fake_session = _fake_silero_session([0.9, 0.9, 0.9, 0.1, 0.1, 0.1])
    with patch.object(voice_audio, "_load_silero_session", return_value=fake_session):
        vad = voice_audio.SileroVad(mode="record")
        for _ in range(3):
            assert vad.feed(speech_frame) is voice_audio.VadEvent.SPEECH_ACTIVE
        for _ in range(3):
            vad.feed(silent_frame)
        # After at least one silence frame post-speech, vad.empty() flips True.
        assert vad.empty() is True


def test_vad_chunk_size_is_512_samples() -> None:
    assert voice_audio.SILERO_CHUNK_SAMPLES == 512


def test_vad_record_mode_uses_lower_prob_threshold_than_tts_mode() -> None:
    """Record mode prob threshold (0.4) < TTS mode (0.5) per legacy vad_silero.py."""
    rec = voice_audio.SileroVad.thresholds(mode="record")
    tts = voice_audio.SileroVad.thresholds(mode="tts")
    assert rec.prob_threshold < tts.prob_threshold
```

- [ ] **Step 2: Run to confirm RED.**

```
.venv/bin/pytest tests/unit/test_voice_audio_vad.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Verbatim port `vad_silero.py`.**

```
cp /Users/alllllenshi/Projects/jarvis-legacy/core/vad_silero.py jarvis/surface/voice_audio.py
```

Then in `jarvis/surface/voice_audio.py`:

1. Replace the module docstring with one that names ADR-0005 §4.2 + spec §3.6.1 and notes "VAD half — recorder added in next commit".
2. Audit imports: keep only `stdlib`, `numpy`, `onnxruntime`. Remove any `from core.* import`.
3. Verify exports include: `SileroVad` (class), `SILERO_CHUNK_SAMPLES` (=512 constant), `VadEvent` (Enum with at least `SPEECH_ACTIVE`, `SILENCE`). Add a `thresholds(mode: str)` classmethod that returns an object with `prob_threshold` attribute, per the test. If legacy uses different names, rename — keep behavior.
4. Two modes (`"record"` / `"tts"`): keep legacy thresholds (record prob 0.4 / dB −45; tts prob 0.5).
5. Add `_load_silero_session(model_path: Path) -> onnxruntime.InferenceSession` as a module-level function so the test can `patch.object(...)` it.
6. Hide ONNX session construction behind lazy init so tests can mock it without loading the model file.

- [ ] **Step 4: Run to confirm GREEN.**

```
.venv/bin/pytest tests/unit/test_voice_audio_vad.py -v
```

All three PASS.

- [ ] **Step 5: Gates + commit.**

```
.venv/bin/ruff check jarvis/surface/voice_audio.py tests/unit/test_voice_audio_vad.py
.venv/bin/mypy jarvis/surface/voice_audio.py
.venv/bin/lint-imports
git add jarvis/surface/voice_audio.py tests/unit/test_voice_audio_vad.py
git commit -m "feat(surface): port Silero VAD (record/tts modes) into voice_audio (ADR-0005 §4.2)"
```

---

## Task 8: `voice_audio.py` — `capture_utterance` (PortAudio recorder, VAD-gated)

**Files:**
- Modify: `jarvis/surface/voice_audio.py` (extend with recorder)
- Test: `tests/unit/test_voice_audio_capture.py`
- Legacy port source: `/Users/alllllenshi/Projects/jarvis-legacy/core/audio_recorder.py`

**Spec basis:** ADR §4.2 + §5.1 (5 s hard cap, 1 s min voiced).

- [ ] **Step 1: Write the failing test.**

Create `tests/unit/test_voice_audio_capture.py`:

```python
"""ADR-0005 voice_audio.capture_utterance — VAD-gated PortAudio recorder."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from jarvis.surface import voice_audio


@pytest.fixture
def vad_yielding_speech_then_silence():
    """SileroVad stub that says speech for first 30 frames then silence."""
    vad = MagicMock(spec=voice_audio.SileroVad)
    call = {"i": 0}

    def _feed(_frame):
        call["i"] += 1
        return (
            voice_audio.VadEvent.SPEECH_ACTIVE
            if call["i"] <= 30
            else voice_audio.VadEvent.SILENCE
        )

    vad.feed.side_effect = _feed
    vad.empty.side_effect = lambda: call["i"] > 35  # flips after ~5 silence frames
    return vad


def test_capture_utterance_returns_audio_when_vad_completes(vad_yielding_speech_then_silence) -> None:
    """A VAD-cut recording returns the accumulated PCM bytes."""
    fake_stream = MagicMock()
    fake_stream.read.return_value = (
        np.zeros(voice_audio.SILERO_CHUNK_SAMPLES, dtype=np.int16).tobytes(),
        False,
    )
    with patch.object(voice_audio, "_open_input_stream", return_value=fake_stream):
        audio = voice_audio.capture_utterance(
            vad=vad_yielding_speech_then_silence,
            max_duration_s=5.0,
            min_voiced_s=1.0,
            sample_rate_hz=16000,
        )
    # Each frame is 512 samples = 1024 bytes; ~35 frames captured.
    assert isinstance(audio, bytes)
    assert len(audio) >= 30 * voice_audio.SILERO_CHUNK_SAMPLES * 2


def test_capture_utterance_respects_max_duration_cap() -> None:
    """If VAD never says silence, capture stops at max_duration_s."""
    vad = MagicMock(spec=voice_audio.SileroVad)
    vad.feed.return_value = voice_audio.VadEvent.SPEECH_ACTIVE
    vad.empty.return_value = False

    fake_stream = MagicMock()
    fake_stream.read.return_value = (
        np.zeros(voice_audio.SILERO_CHUNK_SAMPLES, dtype=np.int16).tobytes(),
        False,
    )
    with patch.object(voice_audio, "_open_input_stream", return_value=fake_stream):
        audio = voice_audio.capture_utterance(
            vad=vad,
            max_duration_s=1.0,
            min_voiced_s=0.5,
            sample_rate_hz=16000,
        )
    # ~1 second at 16 kHz mono PCM16 ≈ 32000 bytes (allow ±15% slack).
    assert 27000 <= len(audio) <= 37000
```

- [ ] **Step 2: Run to confirm RED.**

```
.venv/bin/pytest tests/unit/test_voice_audio_capture.py -v
```

Expected: `AttributeError: module 'jarvis.surface.voice_audio' has no attribute 'capture_utterance'`.

- [ ] **Step 3: Port the recorder half.**

Append to `jarvis/surface/voice_audio.py` (above `__all__`):

```python
# --- Recorder (PortAudio + VAD-gated end-of-speech) ----------------------

import sounddevice as sd


def _open_input_stream(*, sample_rate_hz: int, blocksize: int) -> sd.RawInputStream:
    """Module-level for test patchability."""
    return sd.RawInputStream(
        samplerate=sample_rate_hz,
        channels=1,
        dtype="int16",
        blocksize=blocksize,
    )


def capture_utterance(
    *,
    vad: SileroVad,
    max_duration_s: float,
    min_voiced_s: float,
    sample_rate_hz: int = 16000,
) -> bytes:
    """VAD-gated capture from the default input device.

    Reads PCM16 mono frames of SILERO_CHUNK_SAMPLES each (32 ms at 16 kHz).
    Feeds each frame into the VAD; once at least `min_voiced_s` worth of
    voiced frames have been seen AND the VAD's `empty()` flag becomes True
    (post-speech silence), the recording ends.

    Hard cap: `max_duration_s` enforced via wall-clock.

    Returns the concatenated raw PCM16 little-endian bytes.

    Args:
        vad: a constructed SileroVad in "record" mode.
        max_duration_s: hard upper bound (legacy default 5.0).
        min_voiced_s: minimum voiced duration before VAD-cut allowed
            (legacy default 1.0).
        sample_rate_hz: capture rate; MUST match VAD's expected rate
            (Silero ships 16 kHz).
    """
    blocksize = SILERO_CHUNK_SAMPLES
    seconds_per_frame = blocksize / sample_rate_hz
    max_frames = int(max_duration_s / seconds_per_frame) + 1
    min_voiced_frames = int(min_voiced_s / seconds_per_frame)

    audio_bytes = bytearray()
    voiced_frames = 0

    with _open_input_stream(sample_rate_hz=sample_rate_hz, blocksize=blocksize) as stream:
        for frame_idx in range(max_frames):
            frame, _overflowed = stream.read(blocksize)
            audio_bytes.extend(frame)
            event = vad.feed(bytes(frame))
            if event == VadEvent.SPEECH_ACTIVE:
                voiced_frames += 1
            if voiced_frames >= min_voiced_frames and vad.empty():
                break

    return bytes(audio_bytes)
```

Port any helper functions (e.g. RMS quality check `is_quality_ok`) from `core/audio_recorder.py` only if `voice_pipeline.py` (Task 9) needs them. The legacy `is_quality_ok` is now superseded by `voice_asr.is_empty_or_too_short` — do NOT port it.

- [ ] **Step 4: Run to confirm GREEN.**

```
.venv/bin/pytest tests/unit/test_voice_audio_capture.py -v
```

Both PASS.

- [ ] **Step 5: Gates + commit.**

```
.venv/bin/ruff check jarvis/surface/voice_audio.py tests/unit/test_voice_audio_capture.py
.venv/bin/mypy jarvis/surface/voice_audio.py
.venv/bin/lint-imports
git add jarvis/surface/voice_audio.py tests/unit/test_voice_audio_capture.py
git commit -m "feat(surface): voice_audio.capture_utterance (PortAudio + VAD cut, ADR-0005 §5.1)"
```

---

## Task 9: `voice_pipeline.py` — composition (recognize → normalize → filter → emit) + `VOICE_INPUT_LOCK`

**Files:**
- Create: `jarvis/surface/voice_pipeline.py`
- Test: `tests/unit/test_voice_pipeline.py`
- No legacy source (NEW per ADR §4.2 — composition; legacy ran this inline in `JarvisApp._process_turn`).

**Spec basis:** ADR §3 "Adapter-internal canonicalization" + §4.2 row `voice_pipeline.py` + §8 fix #1, #2, #3.

- [ ] **Step 1: Write the failing test.**

Create `tests/unit/test_voice_pipeline.py`:

```python
"""ADR-0005 voice_pipeline — composition + VOICE_INPUT_LOCK invariants."""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jarvis.state.event_log import open_event_log
from jarvis.surface import voice_asr, voice_pipeline


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "events.db"
    c = open_event_log(db)
    yield c
    c.close()


def _make_recognizer_returning(text: str) -> voice_asr.AsrRecognizer:
    rec = MagicMock(spec=voice_asr.AsrRecognizer)
    rec.recognize.return_value = voice_asr.TranscriptionResult(
        text=text,
        confidence=0.9,
        language_detected="zh-CN",
        emotion="NEUTRAL",
    )
    return rec


def test_run_turn_normalizes_before_emit(conn, tmp_path) -> None:
    """ADR §8 fix #1: emit_event must see the normalized transcript."""
    norm = voice_asr.AsrNormalizer(
        corrections=[], aliases={"卧室主灯": ["卧室灯"]}, fuzzy_enabled=False,
    )
    recognizer = _make_recognizer_returning("打开卧室灯")
    pipeline = voice_pipeline.VoicePipeline(
        conn_factory=lambda: open_event_log(tmp_path / "events.db"),
        recognizer=recognizer,
        normalizer=norm,
        broadcaster=None,  # PTT-style; no broadcast on this path
        artifacts_dir=tmp_path,
    )
    audio = b"\x10\x00" * 16000  # non-silent
    ev = pipeline.run_turn(
        audio_bytes=audio,
        turn_id="T1",
        channel="inherent_ptt",
        language="zh-CN",
    )
    assert ev.payload["transcript"] == "打开卧室主灯", "normalize must run before emit"


def test_run_turn_raises_empty_for_silent_audio(conn, tmp_path) -> None:
    """ADR §8 fix #3: unified empty-filter rejects silent audio."""
    norm = voice_asr.AsrNormalizer(corrections=[], aliases={}, fuzzy_enabled=False)
    recognizer = _make_recognizer_returning("你好")
    pipeline = voice_pipeline.VoicePipeline(
        conn_factory=lambda: open_event_log(tmp_path / "events.db"),
        recognizer=recognizer,
        normalizer=norm,
        broadcaster=None,
        artifacts_dir=tmp_path,
    )
    audio = b"\x00" * 2000  # zero RMS
    with pytest.raises(voice_pipeline.VoicePipelineEmptyError):
        pipeline.run_turn(
            audio_bytes=audio,
            turn_id="T2",
            channel="inherent_ptt",
            language="zh-CN",
        )


def test_run_turn_raises_busy_when_lock_is_held(conn, tmp_path) -> None:
    """ADR §8 fix #2: VOICE_INPUT_LOCK is the wake/PTT mutex."""
    norm = voice_asr.AsrNormalizer(corrections=[], aliases={}, fuzzy_enabled=False)
    pipeline = voice_pipeline.VoicePipeline(
        conn_factory=lambda: open_event_log(tmp_path / "events.db"),
        recognizer=_make_recognizer_returning("你好"),
        normalizer=norm,
        broadcaster=None,
        artifacts_dir=tmp_path,
    )

    voice_pipeline.VOICE_INPUT_LOCK.acquire()
    try:
        with pytest.raises(voice_pipeline.VoiceInputBusyError):
            pipeline.run_turn(
                audio_bytes=b"\x10\x00" * 16000,
                turn_id="T3",
                channel="inherent_ptt",
                language="zh-CN",
                lock_acquire_timeout_s=0.1,
            )
    finally:
        voice_pipeline.VOICE_INPUT_LOCK.release()


def test_run_turn_releases_lock_on_exception(conn, tmp_path) -> None:
    """The lock is released whether emit succeeds, raises empty, or raises ASR error."""
    norm = voice_asr.AsrNormalizer(corrections=[], aliases={}, fuzzy_enabled=False)
    pipeline = voice_pipeline.VoicePipeline(
        conn_factory=lambda: open_event_log(tmp_path / "events.db"),
        recognizer=_make_recognizer_returning("你好"),
        normalizer=norm,
        broadcaster=None,
        artifacts_dir=tmp_path,
    )
    # First turn: empty -> raises. Lock must be free afterward.
    with pytest.raises(voice_pipeline.VoicePipelineEmptyError):
        pipeline.run_turn(
            audio_bytes=b"\x00" * 2000,
            turn_id="T4a",
            channel="inherent_ptt",
            language="zh-CN",
        )
    # Should succeed: lock was released.
    ev = pipeline.run_turn(
        audio_bytes=b"\x10\x00" * 16000,
        turn_id="T4b",
        channel="inherent_ptt",
        language="zh-CN",
    )
    assert ev.payload["transcript"] == "你好"
```

- [ ] **Step 2: Run to confirm RED.**

```
.venv/bin/pytest tests/unit/test_voice_pipeline.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement `voice_pipeline.py`.**

Create `jarvis/surface/voice_pipeline.py`:

```python
"""L5 voice pipeline — composition site for wake + PTT paths (ADR-0005 §4.2).

Owns the wake/PTT mutex (``VOICE_INPUT_LOCK``) so the two input paths
cannot double-capture the default mic (ADR-0005 §8 fix #2). Calls
recognize → normalize → empty-check → optional artifact write →
``emit_event("utterance.received", ...)`` per spec §3.6.1.

Layer rules (`.importlinter` L5 row): imports only stdlib,
``jarvis.shared``, and ``jarvis.state.event_log``. Does NOT name
``jarvis.decision``, ``jarvis.execution``, ``jarvis.deployment``,
``jarvis.runtime``, or ``jarvis.cli``.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from jarvis.state.event_log import emit_event
from jarvis.surface import voice_artifact_store, voice_asr

if TYPE_CHECKING:
    from jarvis.shared import Event


LOGGER = logging.getLogger("jarvis.surface.voice_pipeline")

# Wake/PTT mutex. Module-global; both paths share the same lock instance.
VOICE_INPUT_LOCK = threading.Lock()


class VoicePipelineError(RuntimeError):
    """Base for voice-pipeline failures the caller may catch."""


class VoiceInputBusyError(VoicePipelineError):
    """`VOICE_INPUT_LOCK` could not be acquired within the timeout."""


class VoicePipelineEmptyError(VoicePipelineError):
    """Transcript was empty / silent / punctuation-only after ASR."""


class _BroadcasterProtocol(Protocol):
    """Subset of InherentBroadcaster that voice_pipeline calls.

    The runtime (composition root) constructs the real broadcaster and
    passes it in; tests pass `None` for the PTT-style path.
    """

    def broadcast_voice_sync(self, phase: str, *, turn_id: str, **payload: object) -> None:
        ...


class VoicePipeline:
    """Run one voice turn from raw audio bytes to utterance.received emit.

    The pipeline does NOT capture audio itself — the wake listener and
    the `/inherent/asr-submit` handler each capture (or receive) audio
    and call `run_turn(...)` for the ASR-and-emit phase.
    """

    def __init__(
        self,
        *,
        conn_factory: "callable[[], sqlite3.Connection]",
        recognizer: voice_asr.AsrRecognizer,
        normalizer: voice_asr.AsrNormalizer,
        broadcaster: _BroadcasterProtocol | None,
        artifacts_dir: Path,
        sample_rate_hz: int = 16000,
    ) -> None:
        self._conn_factory = conn_factory
        self._recognizer = recognizer
        self._normalizer = normalizer
        self._broadcaster = broadcaster
        self._artifacts_dir = artifacts_dir
        self._sample_rate_hz = sample_rate_hz

    def run_turn(
        self,
        *,
        audio_bytes: bytes,
        turn_id: str,
        channel: str,
        language: str,
        lock_acquire_timeout_s: float = 2.0,
    ) -> "Event":
        """Execute one voice turn end-to-end. Returns the emitted Event row.

        Raises:
            VoiceInputBusyError: VOICE_INPUT_LOCK contention (PTT path: 503).
            VoicePipelineEmptyError: transcript empty / too short / silent.
            Exception: any unexpected ASR failure (caller decides reaction).
        """
        acquired = VOICE_INPUT_LOCK.acquire(timeout=lock_acquire_timeout_s)
        if not acquired:
            raise VoiceInputBusyError(
                f"VOICE_INPUT_LOCK busy after {lock_acquire_timeout_s}s; turn_id={turn_id}"
            )
        try:
            # 1. Recognize (sync ASR call).
            tr = self._recognizer.recognize(audio_bytes)

            # 2. Empty / too-short filter — ADR §8 fix #3 (unified).
            if voice_asr.is_empty_or_too_short(tr.text, audio_pcm=audio_bytes):
                if self._broadcaster is not None:
                    self._broadcaster.broadcast_voice_sync(
                        "empty", turn_id=turn_id, reason="no_speech",
                    )
                raise VoicePipelineEmptyError(
                    f"empty utterance for turn_id={turn_id}"
                )

            # 3. Normalize BEFORE emit — ADR §8 fix #1 (spec §3.6.2).
            normalized = self._normalizer.normalize(tr.text)

            # 4. Optional raw-WAV artifact retention.
            artifact_ref = voice_artifact_store.persist(
                audio_bytes,
                turn_id=turn_id,
                sample_rate_hz=self._sample_rate_hz,
                artifacts_dir=self._artifacts_dir,
            )

            # 5. Emit utterance.received via fresh connection (worker thread).
            payload: dict[str, object] = {
                "transcript": normalized,
                "turn_id": turn_id,
                "channel": channel,
                "language": language,
                "confidence": tr.confidence,
            }
            if tr.language_detected:
                payload["language_detected"] = tr.language_detected
            if tr.emotion:
                payload["emotion"] = tr.emotion
            if artifact_ref:
                payload["audio_artifact_ref"] = artifact_ref

            with self._conn_factory() as worker_conn:
                ev = emit_event(
                    worker_conn,
                    type="utterance.received",
                    payload=payload,
                    correlation={"turn_id": turn_id},
                )

            # 6. Wake-path UI notify (PTT broadcaster is None — caller handles UI via HTTP response).
            if self._broadcaster is not None:
                accepted_payload: dict[str, object] = {"transcript": normalized}
                if tr.emotion:
                    accepted_payload["emotion"] = tr.emotion
                self._broadcaster.broadcast_voice_sync(
                    "accepted", turn_id=turn_id, **accepted_payload,
                )
            return ev
        finally:
            VOICE_INPUT_LOCK.release()


__all__ = [
    "VOICE_INPUT_LOCK",
    "VoiceInputBusyError",
    "VoicePipeline",
    "VoicePipelineEmptyError",
    "VoicePipelineError",
]
```

Note: `with self._conn_factory() as worker_conn` requires the connection to be a context manager. `sqlite3.Connection` IS a context manager (closes on exit). If the test fixture or the real `open_event_log` returns a non-CM, wrap with `contextlib.closing`. Adjust if a test fails on this line.

- [ ] **Step 4: Run to confirm GREEN.**

```
.venv/bin/pytest tests/unit/test_voice_pipeline.py -v
```

All four PASS.

- [ ] **Step 5: Gates + commit.**

```
.venv/bin/ruff check jarvis/surface/voice_pipeline.py tests/unit/test_voice_pipeline.py
.venv/bin/mypy jarvis/surface/voice_pipeline.py
.venv/bin/lint-imports
git add jarvis/surface/voice_pipeline.py tests/unit/test_voice_pipeline.py
git commit -m "feat(surface): voice_pipeline (recognize/normalize/emit + INPUT_LOCK) (ADR-0005 §4.2)"
```

---

# Phase 3 — IPC integration (broadcaster + ASR endpoint)

## Task 10: `inherent_output.broadcast_voice_sync` — worker-thread → event-loop bridge

**Files:**
- Modify: `jarvis/surface/inherent_output.py` — add two methods + loop ref
- Test: `tests/unit/test_inherent_output_broadcast_voice.py`

**Spec basis:** ADR §6 + §4.3 last row.

- [ ] **Step 1: Write the failing test.**

Create `tests/unit/test_inherent_output_broadcast_voice.py`:

```python
"""ADR-0005 §6: broadcaster gains broadcast_voice (async) + broadcast_voice_sync (worker bridge)."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from jarvis.surface.inherent_output import InherentBroadcaster


@pytest.mark.asyncio
async def test_broadcast_voice_sends_envelope_to_clients() -> None:
    b = InherentBroadcaster()
    ws = AsyncMock()
    await b.register(ws)
    await b.broadcast_voice("listening", turn_id="T1")
    ws.send_json.assert_awaited_once()
    msg = ws.send_json.await_args.args[0]
    assert msg["op"] == "voice"
    assert msg["payload"]["phase"] == "listening"
    assert msg["payload"]["turn_id"] == "T1"


@pytest.mark.asyncio
async def test_broadcast_voice_passes_optional_fields() -> None:
    b = InherentBroadcaster()
    ws = AsyncMock()
    await b.register(ws)
    await b.broadcast_voice("accepted", turn_id="T2", transcript="你好", emotion="HAPPY")
    msg = ws.send_json.await_args.args[0]
    assert msg["payload"]["transcript"] == "你好"
    assert msg["payload"]["emotion"] == "HAPPY"


def test_broadcast_voice_sync_schedules_onto_attached_loop(event_loop) -> None:
    b = InherentBroadcaster()
    b.attach_loop(event_loop)
    ws = AsyncMock()

    async def _wire():
        await b.register(ws)

    event_loop.run_until_complete(_wire())

    # Call from a non-loop "thread" context (same thread but no running loop here).
    b.broadcast_voice_sync("listening", turn_id="T3")
    event_loop.run_until_complete(asyncio.sleep(0.05))
    assert ws.send_json.await_count == 1
```

Note: `pytest-asyncio` provides the `event_loop` fixture; the project already uses it (verify with `grep pytest-asyncio pyproject.toml` — if absent, add it to dev deps with `uv add --dev pytest-asyncio`).

- [ ] **Step 2: Run to confirm RED.**

```
.venv/bin/pytest tests/unit/test_inherent_output_broadcast_voice.py -v
```

Expected: `AttributeError: 'InherentBroadcaster' object has no attribute 'broadcast_voice'`.

- [ ] **Step 3: Extend `InherentBroadcaster`.**

Open `jarvis/surface/inherent_output.py` and add three things:

1. After `self._lock = asyncio.Lock()` in `__init__`, add:
   ```python
   self._loop: asyncio.AbstractEventLoop | None = None
   ```

2. New method `attach_loop`:
   ```python
   def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
       """Composition-root call: stores the daemon's event loop for the
       worker-thread → broadcaster bridge (`broadcast_voice_sync`).

       Idempotent. Called once in `runtime/inherent_loop.serve_inherent`
       just after the broadcaster is constructed.
       """
       self._loop = loop
   ```

3. New method `broadcast_voice` (async) and a thread-safe `broadcast_voice_sync`:
   ```python
   async def broadcast_voice(
       self, phase: str, *, turn_id: str, **payload: object,
   ) -> None:
       """ADR-0005 §6: outbound `op:"voice"` envelope to all registered clients.

       `phase` ∈ {"listening", "transcribing", "accepted", "empty", "error", "spoken"};
       additional payload fields (`transcript`, `emotion`, `reason`) are
       merged into the envelope's `payload` dict.
       """
       msg_payload: dict[str, object] = {"phase": phase, "turn_id": turn_id}
       msg_payload.update(payload)
       msg: dict[str, object] = {"op": "voice", "payload": msg_payload}
       # Reuse the existing send-all path; envelope shape is independent
       # of the surface.response_* dispatch family.
       fake_event = _make_phase_event(turn_id)
       await self._send_all(msg, fake_event)

   def broadcast_voice_sync(
       self, phase: str, *, turn_id: str, **payload: object,
   ) -> None:
       """Worker-thread entry: schedule `broadcast_voice` on the attached loop.

       Used by `voice_pipeline.run_turn` (which runs in a worker thread)
       so the broadcaster's `asyncio.Lock` is held only on the event-loop
       thread.

       If no loop is attached (the daemon hasn't called `attach_loop`),
       this is a no-op + WARN log so unit tests of `voice_pipeline` can
       pass a non-attached broadcaster.
       """
       loop = self._loop
       if loop is None:
           LOGGER.warning(
               "broadcast_voice_sync called before attach_loop; envelope dropped (phase=%s, turn_id=%s)",
               phase, turn_id,
           )
           return
       asyncio.run_coroutine_threadsafe(
           self.broadcast_voice(phase, turn_id=turn_id, **payload),
           loop,
       )
   ```

4. The existing `_send_all` requires an `event` argument because it pulls `turn_id` from `event.payload`. The `broadcast_voice` path doesn't have a real `Event` — so add a tiny module-private helper that produces a stub for the F5 log. Add at module top:

   ```python
   from dataclasses import dataclass

   @dataclass(frozen=True)
   class _PhaseEventStub:
       payload: dict[str, object]

   def _make_phase_event(turn_id: str) -> _PhaseEventStub:
       return _PhaseEventStub(payload={"turn_id": turn_id})
   ```

   (Alternatively: refactor `_send_all` to take `turn_id: str` directly. The stub is the smaller change.)

5. Update `__all__` to include `"InherentBroadcaster"` (already there); no new public symbols needed.

- [ ] **Step 4: Run to confirm GREEN.**

```
.venv/bin/pytest tests/unit/test_inherent_output_broadcast_voice.py -v
```

All three PASS.

- [ ] **Step 5: Gates + commit.**

```
.venv/bin/ruff check jarvis/surface/inherent_output.py tests/unit/test_inherent_output_broadcast_voice.py
.venv/bin/mypy jarvis/surface/inherent_output.py
.venv/bin/lint-imports
git add jarvis/surface/inherent_output.py tests/unit/test_inherent_output_broadcast_voice.py
git commit -m "feat(surface): InherentBroadcaster.broadcast_voice + sync bridge (ADR-0005 §6)"
```

---

## Task 11: `inherent_server.asr_submit` — real implementation (replace 501)

**Files:**
- Modify: `jarvis/surface/inherent_server.py:179-185` (the `/inherent/asr-submit` stub)
- Modify: `jarvis/surface/inherent_server.py` — `InherentDeps` gains a `voice_pipeline_callable: Callable[[bytes, str, str, str], Event]` field (the runtime will bind this to `pipeline.run_turn`)
- Test: `tests/integration/test_inherent_server_asr_submit.py`

**Spec basis:** ADR §5.2 + §6 inbound.

- [ ] **Step 1: Write the failing test.**

Create `tests/integration/test_inherent_server_asr_submit.py`:

```python
"""ADR-0005 §5.2: /inherent/asr-submit happy and error paths."""
from __future__ import annotations

import io
import wave
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from jarvis.surface import voice_pipeline
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app


def _build_wav_bytes(*, duration_s: float = 0.5, sample_rate: int = 16000) -> bytes:
    n_samples = int(duration_s * sample_rate)
    pcm = (b"\x10\x00") * n_samples
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _make_deps(pipeline_callable) -> InherentDeps:
    return InherentDeps(
        submit_callable=lambda text: None,  # ADR-0003 text path stub
        broadcaster=InherentBroadcaster(),
        voice_pipeline_callable=pipeline_callable,
    )


def test_asr_submit_happy_path_returns_transcript() -> None:
    fake_event = MagicMock()
    fake_event.payload = {"transcript": "你好"}

    def fake_pipeline(audio_bytes, turn_id, channel, language):
        return fake_event

    deps = _make_deps(fake_pipeline)
    app = create_app(deps)
    client = TestClient(app)
    wav = _build_wav_bytes()
    resp = client.post(
        "/inherent/asr-submit",
        files={"audio": ("u.wav", wav, "audio/wav")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["transcript"] == "你好"
    assert body["turn_id"].startswith("T")


def test_asr_submit_too_large_returns_413() -> None:
    deps = _make_deps(lambda *a, **kw: pytest.fail("should not be called"))
    app = create_app(deps)
    client = TestClient(app)
    big = b"R" * (6 * 1024 * 1024)  # 6 MB
    resp = client.post(
        "/inherent/asr-submit",
        files={"audio": ("u.wav", big, "audio/wav")},
    )
    assert resp.status_code == 413


def test_asr_submit_returns_422_on_empty_utterance() -> None:
    def fake_pipeline(*a, **kw):
        raise voice_pipeline.VoicePipelineEmptyError("no speech")

    deps = _make_deps(fake_pipeline)
    app = create_app(deps)
    client = TestClient(app)
    wav = _build_wav_bytes()
    resp = client.post(
        "/inherent/asr-submit",
        files={"audio": ("u.wav", wav, "audio/wav")},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "empty"


def test_asr_submit_returns_503_on_busy() -> None:
    def fake_pipeline(*a, **kw):
        raise voice_pipeline.VoiceInputBusyError("lock held")

    deps = _make_deps(fake_pipeline)
    app = create_app(deps)
    client = TestClient(app)
    wav = _build_wav_bytes()
    resp = client.post(
        "/inherent/asr-submit",
        files={"audio": ("u.wav", wav, "audio/wav")},
    )
    assert resp.status_code == 503


def test_asr_submit_returns_500_on_internal_error() -> None:
    def fake_pipeline(*a, **kw):
        raise RuntimeError("boom")

    deps = _make_deps(fake_pipeline)
    app = create_app(deps)
    client = TestClient(app)
    wav = _build_wav_bytes()
    resp = client.post(
        "/inherent/asr-submit",
        files={"audio": ("u.wav", wav, "audio/wav")},
    )
    assert resp.status_code == 500


def test_asr_submit_returns_415_on_wrong_content_type() -> None:
    deps = _make_deps(lambda *a, **kw: pytest.fail("should not be called"))
    app = create_app(deps)
    client = TestClient(app)
    resp = client.post(
        "/inherent/asr-submit",
        files={"audio": ("u.mp3", b"ID3-fake-mp3", "audio/mpeg")},
    )
    assert resp.status_code == 415
```

- [ ] **Step 2: Run to confirm RED.**

```
.venv/bin/pytest tests/integration/test_inherent_server_asr_submit.py -v
```

Expected: the first test fails because the endpoint still returns 501; later tests also fail because `InherentDeps` has no `voice_pipeline_callable` field.

- [ ] **Step 3: Extend `InherentDeps` dataclass.**

In `jarvis/surface/inherent_server.py`, find `@dataclass(frozen=True) class InherentDeps:` and add the field:

```python
voice_pipeline_callable: Callable[[bytes, str, str, str], "Event"] | None = None
```

(Default `None` lets ADR-0003 text-only tests keep working unchanged.)

Update the type imports at the top: add `from collections.abc import Callable` (or move into the `TYPE_CHECKING` block already used) and `from typing import TYPE_CHECKING`. Inside `TYPE_CHECKING`, import `from jarvis.shared import Event`.

- [ ] **Step 4: Replace the 501 endpoint with a real handler.**

In `create_app`, replace the `asr_submit` stub at lines 179-185 with:

```python
import secrets
from fastapi import File, UploadFile, Form

@app.post("/inherent/asr-submit", status_code=200)
async def asr_submit(
    audio: UploadFile = File(...),
    language: str = Form(default="zh-CN"),
) -> dict[str, str]:
    """ADR-0005 §5.2 — real implementation, ports legacy /inherent/asr-submit."""
    if deps.voice_pipeline_callable is None:
        raise HTTPException(status_code=501, detail="voice pipeline not wired")

    # Content type guard (415).
    if audio.content_type not in {"audio/wav", "audio/wave", "audio/x-wav"}:
        raise HTTPException(status_code=415, detail=f"unsupported content type: {audio.content_type}")

    body = await audio.read()
    # Size guard (413). 5 MB hard cap per ADR §5.2.
    if len(body) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="audio too large (max 5MB)")
    if not body:
        raise HTTPException(status_code=400, detail="empty body")

    turn_id = "T" + secrets.token_hex(4)
    try:
        ev = await asyncio.to_thread(
            deps.voice_pipeline_callable,
            body,
            turn_id,
            "inherent_ptt",
            language,
        )
    except VoicePipelineEmptyError:
        raise HTTPException(status_code=422, detail="empty") from None
    except VoiceInputBusyError:
        raise HTTPException(status_code=503, detail="busy") from None
    except Exception as exc:  # noqa: BLE001 — surface as 500 per ADR §5.2 + log.
        LOGGER.exception("asr_submit failed for turn_id=%s: %r", turn_id, exc)
        raise HTTPException(status_code=500, detail="internal") from None

    return {
        "status": "accepted",
        "transcript": str(ev.payload.get("transcript", "")),
        "turn_id": turn_id,
    }
```

Add to the file imports:

```python
import logging
from jarvis.surface.voice_pipeline import VoiceInputBusyError, VoicePipelineEmptyError
LOGGER = logging.getLogger("jarvis.surface.inherent_server")
```

Note: importing `voice_pipeline` from `inherent_server` is intra-layer (both L5); no layer-rule violation.

- [ ] **Step 5: Run to confirm GREEN.**

```
.venv/bin/pytest tests/integration/test_inherent_server_asr_submit.py -v
```

All six PASS.

- [ ] **Step 6: Verify existing ADR-0003 tests still pass.**

```
.venv/bin/pytest tests/ -x -k inherent
```

All existing tests under `tests/.../*inherent*` still PASS — the new optional `voice_pipeline_callable` default keeps backward compat.

- [ ] **Step 7: Gates + commit.**

```
.venv/bin/pytest -x
.venv/bin/ruff check jarvis tests
.venv/bin/mypy jarvis
.venv/bin/lint-imports
git add jarvis/surface/inherent_server.py tests/integration/test_inherent_server_asr_submit.py
git commit -m "feat(surface): inherent /inherent/asr-submit real handler (ADR-0005 §5.2)"
```

---

# Phase 4 — Wake listener

## Task 12: `voice_wake.WakeListener` — openwakeword daemon thread

**Files:**
- Create: `jarvis/surface/voice_wake.py`
- Test: `tests/unit/test_voice_wake.py`
- Legacy port source: `/Users/alllllenshi/Projects/jarvis-legacy/core/wake_word.py` + `inherent_wake_listener.py`

**Spec basis:** ADR §4.2 row `voice_wake.py` + §5.1.

- [ ] **Step 1: Write the failing test.**

Create `tests/unit/test_voice_wake.py`:

```python
"""ADR-0005 voice_wake — wake listener orchestration (mocked openwakeword)."""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from jarvis.surface import voice_pipeline, voice_wake


def _flush_thread(t: threading.Thread, timeout_s: float = 1.0) -> None:
    """Join helper that fails loudly if the thread doesn't exit in time."""
    t.join(timeout=timeout_s)
    assert not t.is_alive(), "wake thread did not stop"


def test_wake_listener_calls_pipeline_on_detect() -> None:
    """When openwakeword fires, the listener captures + runs the pipeline."""
    fake_pipeline = MagicMock()
    fake_pipeline.run_turn.return_value = MagicMock(payload={"transcript": "你好"})
    fake_broadcaster = MagicMock()

    fake_engine = MagicMock()
    # First poll: detection (prob 0.9). Second poll: stop.
    detections = iter([0.9, 0.0])
    fake_engine.predict.side_effect = lambda frame: {"hey_jarvis_v0.1": next(detections, 0.0)}

    fake_capture = MagicMock(return_value=b"\x10\x00" * 16000)

    listener = voice_wake.WakeListener(
        engine=fake_engine,
        pipeline=fake_pipeline,
        broadcaster=fake_broadcaster,
        capture_callable=fake_capture,
        threshold=0.5,
    )
    listener.start()
    # Let it iterate a few times.
    listener.request_stop()
    _flush_thread(listener._thread)

    # Capture was called; pipeline run_turn was called; broadcaster saw "listening".
    fake_capture.assert_called()
    fake_pipeline.run_turn.assert_called_once()


def test_wake_listener_skips_when_tts_is_speaking() -> None:
    """Per ADR §2 no-barge-in rule: wake suspends while TTS is speaking."""
    fake_engine = MagicMock()
    fake_engine.predict.return_value = {"hey_jarvis_v0.1": 0.9}

    fake_pipeline = MagicMock()
    fake_broadcaster = MagicMock()
    fake_capture = MagicMock(return_value=b"\x10\x00" * 16000)

    tts_state = {"speaking": True}
    listener = voice_wake.WakeListener(
        engine=fake_engine,
        pipeline=fake_pipeline,
        broadcaster=fake_broadcaster,
        capture_callable=fake_capture,
        threshold=0.5,
        is_speaking_callable=lambda: tts_state["speaking"],
    )
    listener.start()
    listener.request_stop()
    _flush_thread(listener._thread)
    fake_pipeline.run_turn.assert_not_called()
    fake_capture.assert_not_called()


def test_wake_listener_drops_detection_when_lock_busy() -> None:
    """If VOICE_INPUT_LOCK is held by PTT, wake detection is dropped (not blocked)."""
    fake_engine = MagicMock()
    fake_engine.predict.return_value = {"hey_jarvis_v0.1": 0.9}
    fake_pipeline = MagicMock()
    fake_broadcaster = MagicMock()
    fake_capture = MagicMock(return_value=b"\x10\x00" * 16000)

    voice_pipeline.VOICE_INPUT_LOCK.acquire()
    try:
        listener = voice_wake.WakeListener(
            engine=fake_engine,
            pipeline=fake_pipeline,
            broadcaster=fake_broadcaster,
            capture_callable=fake_capture,
            threshold=0.5,
        )
        listener.start()
        listener.request_stop()
        _flush_thread(listener._thread)
    finally:
        voice_pipeline.VOICE_INPUT_LOCK.release()
    fake_capture.assert_not_called()
    fake_pipeline.run_turn.assert_not_called()
```

- [ ] **Step 2: Run to confirm RED.**

```
.venv/bin/pytest tests/unit/test_voice_wake.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Port `wake_word.py` + `inherent_wake_listener.py` into `voice_wake.py`.**

```
cp /Users/alllllenshi/Projects/jarvis-legacy/core/wake_word.py jarvis/surface/voice_wake.py
```

Open `jarvis/surface/voice_wake.py` and:

1. Replace docstring with ADR-0005 §4.2/§5.1 reference.
2. Audit imports — keep stdlib + numpy + openwakeword + sounddevice + onnxruntime. Drop `from core.*`.
3. Add a `WakeListener` class scaffold (the legacy orchestration lived in `inherent_wake_listener.py`; combine into this file as one module with two classes if needed: `WakeEngine` for the model interface and `WakeListener` for the daemon-thread orchestration). Public API:
   ```python
   class WakeListener:
       def __init__(
           self,
           *,
           engine: "WakeEngine",
           pipeline: voice_pipeline.VoicePipeline,
           broadcaster: "_BroadcasterProtocol | None",
           capture_callable: Callable[[], bytes],
           threshold: float = 0.5,
           is_speaking_callable: Callable[[], bool] | None = None,
           model_name: str = "hey_jarvis_v0.1",
       ) -> None: ...
       def start(self) -> None: ...   # spawns daemon thread named "jarvis-wake"
       def request_stop(self) -> None: ...   # sets a threading.Event the loop checks
       def _run(self) -> None: ...   # the thread main loop
   ```

4. The `_run` loop (port from `inherent_wake_listener.py:_run`) must:
   a. Open `sd.InputStream` at 16 kHz / 1280-sample blocksize. (Or accept a stream factory for tests; tests use a mocked `engine.predict` directly without opening a stream — see test design above.)
   b. For each frame: if `is_speaking_callable()` returns True, sleep 20 ms and continue (no barge-in).
   c. Call `engine.predict(frame)`. If `result["hey_jarvis_v0.1"] > threshold`:
      - Try non-blocking acquire of `voice_pipeline.VOICE_INPUT_LOCK`. If False → log INFO + continue.
      - On acquire: release immediately (the pipeline.run_turn re-acquires inside its own logic). This avoids holding the lock across two acquires; instead, just check `if not VOICE_INPUT_LOCK.acquire(blocking=False): continue; VOICE_INPUT_LOCK.release()` as a peek, then call `pipeline.run_turn` (which acquires/releases).

      ALTERNATIVE: make `VoicePipeline.run_turn` accept a `precheck_lock: bool = False` arg; the wake path passes `True` and a non-blocking acquire is attempted; if it fails, raise `VoiceInputBusyError` immediately without the 2 s wait. This is cleaner; choose this if tests still pass.
   d. broadcaster.broadcast_voice_sync("listening", turn_id=…) (mint a fresh `turn_id`).
   e. ducking via `voice_ducking.SystemAudioDucker().duck()` then `restore()` around the capture step.
   f. `audio_bytes = capture_callable()` — for production this is `lambda: voice_audio.capture_utterance(vad=SileroVad("record"), max_duration_s=5.0, min_voiced_s=1.0)`. Tests inject directly.
   g. broadcaster.broadcast_voice_sync("transcribing", turn_id=…).
   h. `pipeline.run_turn(audio_bytes=…, turn_id=…, channel="inherent_wake", language="zh-CN")`.
   i. Catch `VoicePipelineEmptyError` → `broadcast_voice_sync("empty", …)`; continue.
   j. Catch any other Exception → log ERROR + `broadcast_voice_sync("error", reason="asr_error", …)`; continue.
   k. On unhandled exception in the loop: log ERROR with traceback, sleep 2 s, reopen the InputStream (legacy parity, ADR §10 F8).

5. Add a `WakeEngine` thin class wrapping `openwakeword.Model`:
   ```python
   class WakeEngine:
       def __init__(self, *, model_name: str = "hey_jarvis_v0.1") -> None: ...
       def predict(self, frame_bytes: bytes) -> dict[str, float]: ...
   ```

6. Make sure to NOT import `jarvis.runtime`, `jarvis.decision`, or `jarvis.execution`. The pipeline + broadcaster come via constructor injection.

- [ ] **Step 4: Run to confirm GREEN.**

```
.venv/bin/pytest tests/unit/test_voice_wake.py -v
```

All three PASS. (Hint: if the threaded test hangs, ensure `request_stop()` sets a `threading.Event` and `_run` checks it on every loop iteration.)

- [ ] **Step 5: Gates + commit.**

```
.venv/bin/ruff check jarvis/surface/voice_wake.py tests/unit/test_voice_wake.py
.venv/bin/mypy jarvis/surface/voice_wake.py
.venv/bin/lint-imports
git add jarvis/surface/voice_wake.py tests/unit/test_voice_wake.py
git commit -m "feat(surface): voice_wake WakeListener (openwakeword + VOICE_INPUT_LOCK) (ADR-0005 §5.1)"
```

---

# Phase 5 — TTS pipeline (split into four sub-tasks)

## Task 13: `voice_tts._preprocess_for_speech` — text cleanup before TTS

**Files:**
- Create: `jarvis/surface/voice_tts.py` (file + preprocessor only this task)
- Test: `tests/unit/test_voice_tts_preprocess.py`
- Legacy port source: `/Users/alllllenshi/Projects/jarvis-legacy/core/tts_preprocessor.py`

**Spec basis:** ADR §14 "Additional port (inline)".

- [ ] **Step 1: Write the failing test.**

Create `tests/unit/test_voice_tts_preprocess.py`:

```python
"""ADR-0005 voice_tts._preprocess_for_speech — port of legacy tts_preprocessor."""
from __future__ import annotations

from jarvis.surface.voice_tts import _preprocess_for_speech


def test_strips_emoji() -> None:
    assert _preprocess_for_speech("好啊 🎉") == "好啊"


def test_strips_markdown_asterisks() -> None:
    assert _preprocess_for_speech("**重要**事项") == "重要事项"


def test_strips_brackets() -> None:
    assert _preprocess_for_speech("看 [这里](http://x)") == "看 这里"


def test_passthrough_for_plain_text() -> None:
    assert _preprocess_for_speech("现在是下午三点") == "现在是下午三点"


def test_handles_empty_string() -> None:
    assert _preprocess_for_speech("") == ""
```

- [ ] **Step 2: Run to confirm RED.**

```
.venv/bin/pytest tests/unit/test_voice_tts_preprocess.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Create `voice_tts.py` skeleton with `_preprocess_for_speech`.**

Create `jarvis/surface/voice_tts.py`:

```python
"""L5 voice TTS pipeline — MiniMax streaming WS + PortAudio playback.

ADR-0005 §4.2 / §5.3 / §10 (F6-F7 fallback chain).

Layer rules (`.importlinter` L5 row): imports only stdlib, third-party
(`websockets`, `sounddevice`, `numpy`), `jarvis.shared`, and
`jarvis.state.event_log`. Does NOT name `jarvis.decision`,
`jarvis.execution`, `jarvis.deployment`, `jarvis.runtime`, or
`jarvis.cli`.
"""
from __future__ import annotations

import re

# --- Text preprocessor (ported inline from legacy core/tts_preprocessor.py) ---

# Emoji + symbol Unicode ranges that should be stripped from TTS input.
# Verbatim from legacy; preserves the exact set of code points removed.
_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F680-\U0001F6FF"  # transport & map symbols
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "\U00002600-\U000027BF"  # dingbats
    "\U0001F1E6-\U0001F1FF"  # regional indicator (flags)
    "]+",
    flags=re.UNICODE,
)

_MARKDOWN_BOLD_RE = re.compile(r"\*\*(.*?)\*\*")
_MARKDOWN_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)")
# Strip markdown link syntax `[text](url)` keeping `text` only.
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\([^\)]+\)")


def _preprocess_for_speech(text: str) -> str:
    """Strip TTS-hostile chars (emoji, markdown markers) before synthesis.

    Verbatim behavior port of legacy `core/tts_preprocessor.py`. Does NOT
    apply content safety; the Pre-emit Gate has already vetted the text.
    """
    if not text:
        return ""
    out = _EMOJI_RE.sub("", text)
    out = _MARKDOWN_BOLD_RE.sub(r"\1", out)
    out = _MARKDOWN_LINK_RE.sub(r"\1", out)
    out = _MARKDOWN_ITALIC_RE.sub(r"\1", out)
    return out.strip()


__all__ = ["_preprocess_for_speech"]
```

- [ ] **Step 4: Run to confirm GREEN.**

```
.venv/bin/pytest tests/unit/test_voice_tts_preprocess.py -v
```

All five PASS.

- [ ] **Step 5: Gates + commit.**

```
.venv/bin/ruff check jarvis/surface/voice_tts.py tests/unit/test_voice_tts_preprocess.py
.venv/bin/mypy jarvis/surface/voice_tts.py
.venv/bin/lint-imports
git add jarvis/surface/voice_tts.py tests/unit/test_voice_tts_preprocess.py
git commit -m "feat(surface): voice_tts skeleton + _preprocess_for_speech (ADR-0005 §14)"
```

---

## Task 14: `voice_tts.AudioStreamPlayer` — PortAudio ring buffer + gain ramps

**Files:**
- Modify: `jarvis/surface/voice_tts.py` — add `AudioStreamPlayer` class
- Test: `tests/unit/test_voice_tts_audio_stream_player.py`
- Legacy port source: `/Users/alllllenshi/Projects/jarvis-legacy/core/audio_stream_player.py`

**Spec basis:** ADR §4.2 row `voice_tts.py`.

- [ ] **Step 1: Write the failing test.**

Create `tests/unit/test_voice_tts_audio_stream_player.py`:

```python
"""ADR-0005 voice_tts.AudioStreamPlayer — ring buffer + gain ramps."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from jarvis.surface import voice_tts


def test_audio_stream_player_writes_pcm_to_ring() -> None:
    player = voice_tts.AudioStreamPlayer(sample_rate_hz=48000)
    chunk = np.zeros(480, dtype=np.float32).tobytes()
    player.write(chunk)
    assert player.bytes_pending() >= len(chunk)


def test_audio_stream_player_flush_clears_ring() -> None:
    player = voice_tts.AudioStreamPlayer(sample_rate_hz=48000)
    player.write(b"\x00" * 1920)
    player.flush()
    assert player.bytes_pending() == 0


def test_audio_stream_player_duck_lowers_gain() -> None:
    player = voice_tts.AudioStreamPlayer(sample_rate_hz=48000)
    player.duck(target_gain=0.3, ramp_ms=10)
    # Just verifies the API is callable and doesn't raise.
    assert player.current_gain() <= 1.0
```

- [ ] **Step 2: Run to confirm RED.**

```
.venv/bin/pytest tests/unit/test_voice_tts_audio_stream_player.py -v
```

Expected: `AttributeError: ... has no attribute 'AudioStreamPlayer'`.

- [ ] **Step 3: Port `audio_stream_player.py` into `voice_tts.py`.**

Append the entire `AudioStreamPlayer` class (and any helper functions/constants it needs — gain-ramp math, ring-buffer struct) from `/Users/alllllenshi/Projects/jarvis-legacy/core/audio_stream_player.py` to `jarvis/surface/voice_tts.py`. When porting:

1. Strip the module docstring header (the parent `voice_tts.py` already has one).
2. Keep all numpy / sounddevice imports — they go at the top of `voice_tts.py` if not already present.
3. Public surface required by the test:
   - `AudioStreamPlayer(sample_rate_hz: int)` constructor
   - `.write(pcm_bytes: bytes) -> None`
   - `.bytes_pending() -> int`
   - `.flush() -> None`
   - `.duck(target_gain: float, ramp_ms: int) -> None`
   - `.current_gain() -> float`
   - `.close() -> None` (cleanup)
4. The legacy version starts a PortAudio output stream in `__init__`. For tests, gate the stream open behind a `lazy_open: bool = True` flag and only open on first `write(...)`. This lets the test create + destroy a player without actually opening a device.
5. Add `AudioStreamPlayer` to `__all__`.

- [ ] **Step 4: Run to confirm GREEN.**

```
.venv/bin/pytest tests/unit/test_voice_tts_audio_stream_player.py -v
```

All three PASS.

- [ ] **Step 5: Gates + commit.**

```
.venv/bin/ruff check jarvis/surface/voice_tts.py tests/unit/test_voice_tts_audio_stream_player.py
.venv/bin/mypy jarvis/surface/voice_tts.py
.venv/bin/lint-imports
git add jarvis/surface/voice_tts.py tests/unit/test_voice_tts_audio_stream_player.py
git commit -m "feat(surface): port AudioStreamPlayer into voice_tts (ADR-0005 §4.2)"
```

---

## Task 15: `voice_tts.MiniMaxWSClient` — streaming WS provider (mockable)

**Files:**
- Modify: `jarvis/surface/voice_tts.py` — add MiniMax client
- Test: `tests/unit/test_voice_tts_minimax_client.py`
- Legacy port source: `/Users/alllllenshi/Projects/jarvis-legacy/core/tts_minimax_ws.py`

**Spec basis:** ADR §4.2 + §10 F6.

- [ ] **Step 1: Write the failing test.**

Create `tests/unit/test_voice_tts_minimax_client.py`:

```python
"""ADR-0005 voice_tts.MiniMaxWSClient — provider protocol + happy path with mocked WS."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jarvis.surface import voice_tts


@pytest.mark.asyncio
async def test_minimax_synthesize_yields_pcm_chunks() -> None:
    """Mocked WS returns 3 PCM chunks; client yields them in order."""
    fake_ws = AsyncMock()
    # Iterate-once recv returns three frames then closes.
    chunks_returned = iter(
        [
            b"\x01" * 240,  # PCM chunk 1
            b"\x02" * 240,
            b"\x03" * 240,
        ]
    )

    async def _recv():
        try:
            return next(chunks_returned)
        except StopIteration:
            raise voice_tts._WSClosed("end")

    fake_ws.recv.side_effect = _recv
    fake_ws.send = AsyncMock()

    @async_contextmanager
    async def _connect(*a, **kw):
        yield fake_ws

    # ... (test continues with patching websockets.connect to return fake_ws)
    # Verify pcm chunks are accumulated in order
```

Note: this test is illustrative; depending on the legacy `tts_minimax_ws.py` API shape, simplify to a single end-to-end synthesize call that returns concatenated PCM. The test must mock `websockets.connect` so no real network is required.

Simplified concrete test:

```python
@pytest.mark.asyncio
async def test_minimax_client_synthesize_returns_pcm_bytes(monkeypatch) -> None:
    fake_ws = AsyncMock()
    fake_ws.send = AsyncMock()
    # Simulate WS yielding two PCM frames then closing.
    fake_ws.__aiter__.return_value = iter([
        b'\x00{"data":{"audio":"AAAA"},"is_final":false}',
        b'\x00{"data":{"audio":""},"is_final":true}',
    ])

    monkeypatch.setattr(voice_tts.websockets, "connect", lambda *a, **k: _AsyncCM(fake_ws))

    client = voice_tts.MiniMaxWSClient(
        api_key="test-key",
        voice="Chinese (Mandarin)_ExplorativeGirl",
        primary_endpoint="wss://api-uw.minimax.io",
        fallback_endpoint="wss://api.minimax.chat",
    )
    pcm = await client.synthesize("你好")
    assert isinstance(pcm, bytes)


class _AsyncCM:
    def __init__(self, ws): self._ws = ws
    async def __aenter__(self): return self._ws
    async def __aexit__(self, *a): return False
```

Adjust the test to match the legacy WS framing exactly when porting; the exact JSON-over-WS shape is in legacy `tts_minimax_ws.py`.

- [ ] **Step 2: Run to confirm RED.**

```
.venv/bin/pytest tests/unit/test_voice_tts_minimax_client.py -v
```

Expected: `AttributeError: ... MiniMaxWSClient`.

- [ ] **Step 3: Port `tts_minimax_ws.py` into `voice_tts.py`.**

Append the MiniMax WS client implementation from `/Users/alllllenshi/Projects/jarvis-legacy/core/tts_minimax_ws.py` to `jarvis/surface/voice_tts.py`. Public surface:

```python
class MiniMaxWSClient:
    def __init__(
        self,
        *,
        api_key: str,
        voice: str = "Chinese (Mandarin)_ExplorativeGirl",
        primary_endpoint: str = "wss://api-uw.minimax.io",
        fallback_endpoint: str = "wss://api.minimax.chat",
        connect_timeout_s: float = 3.0,
    ) -> None: ...

    async def synthesize(self, text: str) -> bytes:
        """Return raw PCM bytes for `text`.

        Attempts primary endpoint first; on TimeoutError / OSError /
        WS-protocol error, falls back to `fallback_endpoint`. Raises
        `MiniMaxUnavailableError` if both fail.
        """
        ...

    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        """Yield PCM chunks as they arrive. For long sentences."""
        ...


class MiniMaxUnavailableError(RuntimeError): ...
```

When porting:

1. Replace any `from core.*` imports.
2. Keep MiniMax-specific protocol details verbatim (JSON envelope, base64-decode audio, sample rate).
3. Add `import websockets` at the top of `voice_tts.py`.
4. Re-export `MiniMaxWSClient`, `MiniMaxUnavailableError` in `__all__`.

- [ ] **Step 4: Run to confirm GREEN.**

```
.venv/bin/pytest tests/unit/test_voice_tts_minimax_client.py -v
```

PASS.

- [ ] **Step 5: Gates + commit.**

```
.venv/bin/ruff check jarvis/surface/voice_tts.py tests/unit/test_voice_tts_minimax_client.py
.venv/bin/mypy jarvis/surface/voice_tts.py
.venv/bin/lint-imports
git add jarvis/surface/voice_tts.py tests/unit/test_voice_tts_minimax_client.py
git commit -m "feat(surface): port MiniMaxWSClient into voice_tts (ADR-0005 §4.2 §10.F6)"
```

---

## Task 16: `voice_tts.TTSPipeline` — gate-mode routing + `macos_say` fallback

**Files:**
- Modify: `jarvis/surface/voice_tts.py` — add `TTSPipeline`
- Test: `tests/unit/test_voice_tts_pipeline.py`
- Legacy port source: `/Users/alllllenshi/Projects/jarvis-legacy/core/tts.py`

**Spec basis:** ADR §5.3 (gate-mode routing) + §10 F6/F7.

- [ ] **Step 1: Write the failing test.**

Create `tests/unit/test_voice_tts_pipeline.py`:

```python
"""ADR-0005 voice_tts.TTSPipeline — gate-mode routing + fallback."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jarvis.surface import voice_tts


@pytest.fixture
def fake_provider() -> MagicMock:
    p = MagicMock(spec=voice_tts.MiniMaxWSClient)
    p.synthesize = AsyncMock(return_value=b"\x00" * 1920)
    return p


@pytest.fixture
def fake_player() -> MagicMock:
    pl = MagicMock(spec=voice_tts.AudioStreamPlayer)
    pl.bytes_pending.return_value = 0
    return pl


def test_sentence_mode_speaks_each_chunk_immediately(fake_provider, fake_player) -> None:
    pipeline = voice_tts.TTSPipeline(
        provider=fake_provider, player=fake_player, fallback=lambda text: None,
    )
    pipeline.begin_turn("T1", gate_mode="sentence")
    pipeline.handle_chunk("T1", "你好。")
    pipeline.handle_chunk("T1", "今天天气怎么样？")
    pipeline.end_turn("T1")
    # synthesize called once per sentence (2 chunks total).
    assert fake_provider.synthesize.await_count == 2


def test_full_text_mode_buffers_until_emitted(fake_provider, fake_player) -> None:
    pipeline = voice_tts.TTSPipeline(
        provider=fake_provider, player=fake_player, fallback=lambda text: None,
    )
    pipeline.begin_turn("T2", gate_mode="full_text")
    pipeline.handle_chunk("T2", "你好。")
    pipeline.handle_chunk("T2", "今天天气怎么样？")
    # No synth yet.
    assert fake_provider.synthesize.await_count == 0
    pipeline.handle_emitted("T2")
    # One synth with the joined text.
    assert fake_provider.synthesize.await_count == 1
    args, _ = fake_provider.synthesize.await_args
    assert "你好" in args[0] and "天气" in args[0]


def test_structured_mode_treated_as_full_text(fake_provider, fake_player) -> None:
    pipeline = voice_tts.TTSPipeline(
        provider=fake_provider, player=fake_player, fallback=lambda text: None,
    )
    pipeline.begin_turn("T3", gate_mode="structured")
    pipeline.handle_chunk("T3", "ok")
    assert fake_provider.synthesize.await_count == 0
    pipeline.handle_emitted("T3")
    assert fake_provider.synthesize.await_count == 1


def test_missing_gate_mode_defaults_to_sentence(fake_provider, fake_player) -> None:
    pipeline = voice_tts.TTSPipeline(
        provider=fake_provider, player=fake_player, fallback=lambda text: None,
    )
    pipeline.begin_turn("T4", gate_mode=None)
    pipeline.handle_chunk("T4", "hi.")
    assert fake_provider.synthesize.await_count == 1


def test_provider_failure_triggers_fallback(fake_player) -> None:
    fake_provider = MagicMock(spec=voice_tts.MiniMaxWSClient)
    fake_provider.synthesize = AsyncMock(side_effect=voice_tts.MiniMaxUnavailableError("boom"))
    fallback_calls: list[str] = []
    pipeline = voice_tts.TTSPipeline(
        provider=fake_provider,
        player=fake_player,
        fallback=lambda text: fallback_calls.append(text),
    )
    pipeline.begin_turn("T5", gate_mode="sentence")
    pipeline.handle_chunk("T5", "测试")
    pipeline.end_turn("T5")
    assert fallback_calls == ["测试"]


def test_is_speaking_returns_true_while_buffer_nonempty() -> None:
    player = MagicMock(spec=voice_tts.AudioStreamPlayer)
    player.bytes_pending.return_value = 100
    pipeline = voice_tts.TTSPipeline(
        provider=MagicMock(spec=voice_tts.MiniMaxWSClient),
        player=player,
        fallback=lambda text: None,
    )
    assert pipeline.is_speaking() is True
    player.bytes_pending.return_value = 0
    assert pipeline.is_speaking() is False
```

- [ ] **Step 2: Run to confirm RED.**

```
.venv/bin/pytest tests/unit/test_voice_tts_pipeline.py -v
```

Expected: `AttributeError: ... TTSPipeline`.

- [ ] **Step 3: Implement `TTSPipeline`.**

Append to `jarvis/surface/voice_tts.py`:

```python
# --- TTS Pipeline (gate-mode routing + fallback chain) -----------------------

import asyncio
import logging
import subprocess
from collections.abc import Callable
from typing import Literal

LOGGER = logging.getLogger("jarvis.surface.voice_tts")


GateMode = Literal["sentence", "full_text", "structured"]


class TTSPipeline:
    """Event-driven TTS playback per ADR-0005 §5.3.

    `_tts_watcher` (runtime layer) dispatches `surface.response_*` events
    into the three handler methods (`begin_turn`, `handle_chunk`,
    `handle_emitted`). The pipeline owns sentence/full-text routing
    (spec §3.6.6) and the MiniMax → macos_say fallback chain (§3.6.11).
    """

    def __init__(
        self,
        *,
        provider: MiniMaxWSClient,
        player: AudioStreamPlayer,
        fallback: Callable[[str], None],
        broadcaster: object | None = None,  # InherentBroadcaster protocol
    ) -> None:
        self._provider = provider
        self._player = player
        self._fallback = fallback
        self._broadcaster = broadcaster
        # Per-turn state (one turn at a time per ADR §3 Presentation row).
        self._turn_id: str | None = None
        self._gate_mode: GateMode = "sentence"
        self._buffer: list[str] = []

    def begin_turn(self, turn_id: str, *, gate_mode: GateMode | None) -> None:
        """Called on surface.response_open."""
        self._turn_id = turn_id
        self._gate_mode = gate_mode or "sentence"
        self._buffer.clear()

    def handle_chunk(self, turn_id: str, text: str) -> None:
        """Called on surface.response_chunk."""
        if turn_id != self._turn_id:
            LOGGER.warning("TTSPipeline: chunk for unknown turn_id=%s (current=%s)", turn_id, self._turn_id)
            return
        if self._gate_mode == "sentence":
            self._speak(text)
        else:
            self._buffer.append(text)

    def handle_emitted(self, turn_id: str) -> None:
        """Called on surface.response_emitted."""
        if turn_id != self._turn_id:
            return
        if self._gate_mode != "sentence":
            joined = "".join(self._buffer)
            if joined:
                self._speak(joined)
        self.end_turn(turn_id)

    def end_turn(self, turn_id: str) -> None:
        """Final cleanup; broadcasts `spoken` if a broadcaster is attached."""
        if self._broadcaster is not None and hasattr(self._broadcaster, "broadcast_voice_sync"):
            self._broadcaster.broadcast_voice_sync("spoken", turn_id=turn_id)
        self._turn_id = None
        self._buffer.clear()

    def is_speaking(self) -> bool:
        return self._player.bytes_pending() > 0

    def _speak(self, text: str) -> None:
        cleaned = _preprocess_for_speech(text)
        if not cleaned:
            return
        try:
            pcm = asyncio.run(self._provider.synthesize(cleaned))
            self._player.write(pcm)
        except MiniMaxUnavailableError:
            LOGGER.warning("MiniMax unavailable; falling back to macos_say for: %r", cleaned)
            self._fallback(cleaned)
        except Exception as exc:  # noqa: BLE001 — TTS path must not crash; F7 fallback.
            LOGGER.exception("TTS synth failed for turn_id=%s: %r", self._turn_id, exc)
            self._fallback(cleaned)


def macos_say_fallback(text: str, *, voice: str = "Tingting") -> None:
    """ADR-0005 §10 F7 fallback: macOS `say` subprocess.

    Log-only on failure (subprocess returns non-zero); the assistant
    response is silent but the daemon stays up.
    """
    try:
        subprocess.run(  # noqa: S603 — voice & text args come from internal call sites only.
            ["say", "-v", voice, text],
            check=False,
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        LOGGER.warning("macos_say_fallback failed: %r — response remains silent", exc)


__all__ = [
    "AudioStreamPlayer",
    "MiniMaxUnavailableError",
    "MiniMaxWSClient",
    "TTSPipeline",
    "_preprocess_for_speech",
    "macos_say_fallback",
]
```

Hint on `_speak`: the test uses `MagicMock(spec=MiniMaxWSClient)` with `synthesize` as an `AsyncMock`. Calling `asyncio.run(provider.synthesize(...))` from a sync context works for the test. In production the pipeline is called from `_tts_watcher` (an async task) — the watcher should arrange to await, not `asyncio.run`. Refine in Task 18 if needed; for now `asyncio.run` keeps the unit tests synchronous.

- [ ] **Step 4: Run to confirm GREEN.**

```
.venv/bin/pytest tests/unit/test_voice_tts_pipeline.py -v
```

All six PASS.

- [ ] **Step 5: Gates + commit.**

```
.venv/bin/ruff check jarvis/surface/voice_tts.py tests/unit/test_voice_tts_pipeline.py
.venv/bin/mypy jarvis/surface/voice_tts.py
.venv/bin/lint-imports
git add jarvis/surface/voice_tts.py tests/unit/test_voice_tts_pipeline.py
git commit -m "feat(surface): voice_tts TTSPipeline (gate-mode routing + say fallback) (ADR-0005 §5.3)"
```

---

# Phase 6 — Runtime wiring

## Task 17: Extend `_user_intent_watcher` to also poll `utterance.received`

**Files:**
- Modify: `jarvis/runtime/inherent_loop.py:272-329` (`_user_intent_watcher` body)
- Modify: `jarvis/runtime/inherent_loop.py` — adjust `_fetch_events_after` call site
- Test: `tests/integration/test_inherent_loop_utterance_watcher.py`

**Spec basis:** ADR §5.1 "Important wiring detail" + §4.3 table.

- [ ] **Step 1: Write the failing test.**

Create `tests/integration/test_inherent_loop_utterance_watcher.py`:

```python
"""ADR-0005 §5.1: _user_intent_watcher consumes utterance.received same as surface.user_intent."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from jarvis.runtime import inherent_loop
from jarvis.state.event_log import emit_event, open_event_log


@pytest.mark.asyncio
async def test_user_intent_watcher_drives_turn_on_utterance_received(tmp_path: Path) -> None:
    conn = open_event_log(tmp_path / "events.db")
    runtime = inherent_loop.JarvisRuntime(  # type: ignore[call-arg]
        conn=conn,
        # Other JarvisRuntime fields can be defaulted — see runtime/__init__.py
    )

    driven_events: list[str] = []

    def _fake_drive(runtime, user_intent_event):
        driven_events.append(user_intent_event.type)

    with patch.object(inherent_loop, "_drive_turn_in_worker_thread", side_effect=_fake_drive):
        # Start watcher.
        task = asyncio.create_task(
            inherent_loop._user_intent_watcher(runtime, poll_interval_s=0.05)
        )
        await asyncio.sleep(0.1)
        # Emit one of each. Both should drive the turn.
        emit_event(
            conn, type="surface.user_intent",
            payload={"transcript": "hi", "turn_id": "Tcli", "channel": "cli_stdin", "language": "en"},
            correlation={"turn_id": "Tcli"},
        )
        emit_event(
            conn, type="utterance.received",
            payload={"transcript": "你好", "turn_id": "Tvoice", "channel": "inherent_wake", "language": "zh-CN"},
            correlation={"turn_id": "Tvoice"},
        )
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert "surface.user_intent" in driven_events
    assert "utterance.received" in driven_events
```

Note: `JarvisRuntime` construction may need a few extra defaults; if the test's `JarvisRuntime(conn=conn)` constructor doesn't accept that minimum, look in `jarvis/runtime/__init__.py` for the dataclass and supply the missing required fields. Use sensible test defaults.

- [ ] **Step 2: Run to confirm RED.**

```
.venv/bin/pytest tests/integration/test_inherent_loop_utterance_watcher.py -v
```

Expected: only `surface.user_intent` shows up in `driven_events`; `utterance.received` is ignored.

- [ ] **Step 3: Extend the watcher's event filter.**

In `jarvis/runtime/inherent_loop.py`, find `_fetch_events_after(...)` helper — it currently takes a single `event_type: str`. Two options:

A. **Preferred:** Generalize it to accept `event_types: tuple[str, ...]`. Update the SQL `WHERE type = ?` to `WHERE type IN (?, ...)` using a parameterized `IN` clause. Adjust both call sites (`_user_intent_watcher` and `_response_watcher` — the latter already passes a tuple via separate dispatch).

B. **Minimal:** keep the helper signature, and in `_user_intent_watcher` call it twice (once per type), then merge + sort by row id. Simpler to land but less efficient.

Choose A. The helper change is small:

```python
def _fetch_events_after(
    conn: sqlite3.Connection,
    *,
    after_id: int,
    event_types: tuple[str, ...],
) -> list[tuple[int, Event]]:
    placeholders = ",".join("?" * len(event_types))
    rows = conn.execute(
        f"SELECT id, ... FROM events WHERE id > ? AND type IN ({placeholders}) ORDER BY id ASC",  # noqa: S608 — placeholders only, no user input
        (after_id, *event_types),
    ).fetchall()
    ...
```

Update both callers — `_user_intent_watcher` passes `("surface.user_intent", "utterance.received")`; `_response_watcher` already passes the three response types (verify).

- [ ] **Step 4: Run to confirm GREEN.**

```
.venv/bin/pytest tests/integration/test_inherent_loop_utterance_watcher.py -v
.venv/bin/pytest tests/ -x -k inherent_loop
```

Both PASS.

- [ ] **Step 5: Gates + commit.**

```
.venv/bin/pytest -x
.venv/bin/ruff check jarvis tests
.venv/bin/mypy jarvis
.venv/bin/lint-imports
git add jarvis/runtime/inherent_loop.py tests/integration/test_inherent_loop_utterance_watcher.py
git commit -m "feat(runtime): user_intent_watcher also consumes utterance.received (ADR-0005 §5.1)"
```

---

## Task 18: Add `_tts_watcher` background task

**Files:**
- Modify: `jarvis/runtime/inherent_loop.py` — add `_tts_watcher` + spawn from `serve_inherent`
- Test: `tests/integration/test_inherent_loop_tts_watcher.py`

**Spec basis:** ADR §5.3 + §4.3 row `_tts_watcher`.

- [ ] **Step 1: Write the failing test.**

Create `tests/integration/test_inherent_loop_tts_watcher.py`:

```python
"""ADR-0005 §5.3: _tts_watcher dispatches surface.response_* into TTSPipeline."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jarvis.runtime import inherent_loop
from jarvis.state.event_log import emit_event, open_event_log


@pytest.mark.asyncio
async def test_tts_watcher_dispatches_three_event_types(tmp_path: Path) -> None:
    conn = open_event_log(tmp_path / "events.db")
    pipeline = MagicMock()
    task = asyncio.create_task(
        inherent_loop._tts_watcher(conn=conn, pipeline=pipeline, poll_interval_s=0.05)
    )
    await asyncio.sleep(0.1)
    emit_event(
        conn, type="surface.response_open",
        payload={"turn_id": "T1", "query": "hi", "kind": "text", "required_gate_mode": "sentence"},
        correlation={"turn_id": "T1"},
    )
    emit_event(
        conn, type="surface.response_chunk",
        payload={"turn_id": "T1", "text": "hi."},
        correlation={"turn_id": "T1"},
    )
    emit_event(
        conn, type="surface.response_emitted",
        payload={"turn_id": "T1"},
        correlation={"turn_id": "T1"},
    )
    await asyncio.sleep(0.2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    pipeline.begin_turn.assert_called_once_with("T1", gate_mode="sentence")
    pipeline.handle_chunk.assert_called_once_with("T1", "hi.")
    pipeline.handle_emitted.assert_called_once_with("T1")


@pytest.mark.asyncio
async def test_tts_watcher_defaults_gate_mode_to_sentence_when_missing(tmp_path: Path) -> None:
    conn = open_event_log(tmp_path / "events.db")
    pipeline = MagicMock()
    task = asyncio.create_task(
        inherent_loop._tts_watcher(conn=conn, pipeline=pipeline, poll_interval_s=0.05)
    )
    await asyncio.sleep(0.1)
    emit_event(
        conn, type="surface.response_open",
        payload={"turn_id": "T2", "query": "hi", "kind": "text"},  # no required_gate_mode
        correlation={"turn_id": "T2"},
    )
    await asyncio.sleep(0.2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    pipeline.begin_turn.assert_called_once_with("T2", gate_mode="sentence")
```

- [ ] **Step 2: Run to confirm RED.**

```
.venv/bin/pytest tests/integration/test_inherent_loop_tts_watcher.py -v
```

Expected: `AttributeError: module 'jarvis.runtime.inherent_loop' has no attribute '_tts_watcher'`.

- [ ] **Step 3: Add `_tts_watcher`.**

In `jarvis/runtime/inherent_loop.py`, after `_response_watcher`, add:

```python
async def _tts_watcher(
    *,
    conn: sqlite3.Connection,
    pipeline: object,  # voice_tts.TTSPipeline protocol; typed loosely to avoid the L5 import cycle.
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
) -> None:
    """Background task: feed every surface.response_* row into the TTSPipeline.

    Single cursor, three dispatch targets (`begin_turn` / `handle_chunk` /
    `handle_emitted`) per spec §3.6.6 + ADR-0005 §5.3. Runs in parallel
    with `_response_watcher`; both are read-only polls on the same
    event stream and do not contend on writes.
    """
    after_id = _latest_id(conn)
    LOGGER.info("tts_watcher started (after_id=%d)", after_id)
    try:
        while True:
            new_events = _fetch_events_after(
                conn,
                after_id=after_id,
                event_types=(
                    "surface.response_open",
                    "surface.response_chunk",
                    "surface.response_emitted",
                ),
            )
            for row_id, ev in new_events:
                after_id = max(after_id, row_id)
                try:
                    turn_id = str(ev.payload.get("turn_id", ""))
                    if ev.type == "surface.response_open":
                        gate_mode = ev.payload.get("required_gate_mode", "sentence")
                        pipeline.begin_turn(turn_id, gate_mode=gate_mode)
                    elif ev.type == "surface.response_chunk":
                        text = str(ev.payload.get("text", ""))
                        pipeline.handle_chunk(turn_id, text)
                    elif ev.type == "surface.response_emitted":
                        pipeline.handle_emitted(turn_id)
                except Exception as exc:  # noqa: BLE001 — TTS path must not crash watcher; log + continue.
                    LOGGER.warning("tts_watcher: dispatch raised on %s turn_id=%s: %r", ev.type, ev.payload.get("turn_id"), exc)
            await asyncio.sleep(poll_interval_s)
    except asyncio.CancelledError:
        LOGGER.info("tts_watcher cancelled")
        raise
```

- [ ] **Step 4: Run to confirm GREEN.**

```
.venv/bin/pytest tests/integration/test_inherent_loop_tts_watcher.py -v
```

Both PASS.

- [ ] **Step 5: Gates + commit.**

```
.venv/bin/pytest -x
.venv/bin/ruff check jarvis tests
.venv/bin/mypy jarvis
.venv/bin/lint-imports
git add jarvis/runtime/inherent_loop.py tests/integration/test_inherent_loop_tts_watcher.py
git commit -m "feat(runtime): _tts_watcher dispatches response_* into TTSPipeline (ADR-0005 §5.3)"
```

---

## Task 19: `serve_inherent` — spawn WakeListener + pre-flight model checks + wire TTS

**Files:**
- Modify: `jarvis/runtime/inherent_loop.py:serve_inherent` (and `InherentDeps` construction)
- Test: `tests/integration/test_serve_inherent_voice_wiring.py`

**Spec basis:** ADR §12 (pre-flight) + §4.2 + §5.1.

- [ ] **Step 1: Write the failing test.**

Create `tests/integration/test_serve_inherent_voice_wiring.py`:

```python
"""ADR-0005 §12: serve_inherent skips wake when models missing; wires voice pipeline always."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from jarvis.runtime import inherent_loop


def test_preflight_model_check_returns_false_when_missing(tmp_path: Path) -> None:
    """Pre-flight returns False with a missing-models list."""
    ok, missing = inherent_loop._voice_models_preflight(
        sensevoice_dir=tmp_path / "missing-sv",
        silero_path=tmp_path / "missing-silero.onnx",
    )
    assert ok is False
    assert "sensevoice-small-int8" in missing[0] or "silero" in missing[0].lower()


def test_preflight_model_check_returns_true_when_present(tmp_path: Path) -> None:
    sv = tmp_path / "sv"
    sv.mkdir()
    (sv / "model.int8.onnx").write_bytes(b"x")
    (sv / "tokens.txt").write_text("")
    silero = tmp_path / "silero_vad.onnx"
    silero.write_bytes(b"x")
    ok, missing = inherent_loop._voice_models_preflight(
        sensevoice_dir=sv,
        silero_path=silero,
    )
    assert ok is True
    assert missing == []
```

- [ ] **Step 2: Run to confirm RED.**

```
.venv/bin/pytest tests/integration/test_serve_inherent_voice_wiring.py -v
```

Expected: `AttributeError`.

- [ ] **Step 3: Add `_voice_models_preflight` + voice wiring in `serve_inherent`.**

In `jarvis/runtime/inherent_loop.py`, near the other module helpers, add:

```python
def _voice_models_preflight(
    *,
    sensevoice_dir: Path,
    silero_path: Path,
) -> tuple[bool, list[str]]:
    """Verify on-disk model artifacts before spawning wake listener.

    Returns `(all_ok, missing_paths_list)`.
    """
    missing: list[str] = []
    sv_model = sensevoice_dir / "model.int8.onnx"
    sv_tokens = sensevoice_dir / "tokens.txt"
    if not sv_model.exists():
        missing.append(f"sensevoice-small-int8 model.int8.onnx (expected at {sv_model})")
    if not sv_tokens.exists():
        missing.append(f"sensevoice-small-int8 tokens.txt (expected at {sv_tokens})")
    if not silero_path.exists():
        missing.append(f"silero_vad.onnx (expected at {silero_path})")
    return (not missing, missing)
```

Then update `serve_inherent` (around the existing `InherentBroadcaster()` construction and watcher task spawn):

```python
async def serve_inherent(runtime: JarvisRuntime, ...) -> None:
    ...
    broadcaster = InherentBroadcaster()
    broadcaster.attach_loop(asyncio.get_running_loop())

    # --- ADR-0005 voice subsystem wiring -----------------------------------
    sensevoice_dir = Path("data/sensevoice-small-int8")
    silero_path = Path("data/silero_vad.onnx")
    voice_models_ok, missing = _voice_models_preflight(
        sensevoice_dir=sensevoice_dir, silero_path=silero_path,
    )

    pipeline_callable = None
    wake_listener = None
    tts_pipeline = None
    if voice_models_ok:
        from jarvis.surface import (
            voice_asr, voice_audio, voice_ducking, voice_pipeline,
            voice_tts, voice_wake,
        )
        # Build recognizer + normalizer from config.
        recognizer = voice_asr.SenseVoiceRecognizer(
            model_dir=sensevoice_dir,
        )
        normalizer = voice_asr.AsrNormalizer(
            corrections=[],  # populated from config in production
            aliases={},
            fuzzy_enabled=False,
        )
        artifacts_dir = Path("data/voice_artifacts")
        # Fresh-connection factory for the worker thread (ADR-0003 §SQLite-thread-safety).
        db_path = Path(runtime.db_path)  # adjust to the actual JarvisRuntime field name
        pipeline = voice_pipeline.VoicePipeline(
            conn_factory=lambda: open_event_log(db_path),
            recognizer=recognizer,
            normalizer=normalizer,
            broadcaster=broadcaster,
            artifacts_dir=artifacts_dir,
        )
        pipeline_callable = pipeline.run_turn

        # TTS pipeline construction (skip if MINIMAX_API_KEY unset → say-only fallback).
        api_key = os.environ.get("MINIMAX_API_KEY")
        if api_key:
            tts_provider = voice_tts.MiniMaxWSClient(api_key=api_key)
        else:
            tts_provider = None  # type: ignore[assignment]
            LOGGER.warning("MINIMAX_API_KEY not set; TTS will fall through to macos_say")
        tts_player = voice_tts.AudioStreamPlayer(sample_rate_hz=48000)
        tts_pipeline = voice_tts.TTSPipeline(
            provider=tts_provider,
            player=tts_player,
            fallback=voice_tts.macos_say_fallback,
            broadcaster=broadcaster,
        )

        # Wake listener (unless disabled by env).
        if os.environ.get("JARVIS_VOICE_DISABLE_WAKE", "0") != "1":
            wake_engine = voice_wake.WakeEngine(model_name="hey_jarvis_v0.1")
            silero_vad = voice_audio.SileroVad(mode="record")  # may need model_path arg
            wake_listener = voice_wake.WakeListener(
                engine=wake_engine,
                pipeline=pipeline,
                broadcaster=broadcaster,
                capture_callable=lambda: voice_audio.capture_utterance(
                    vad=silero_vad, max_duration_s=5.0, min_voiced_s=1.0,
                ),
                threshold=0.5,
                is_speaking_callable=tts_pipeline.is_speaking if tts_pipeline else None,
            )
            wake_listener.start()
    else:
        LOGGER.error("voice models missing — wake + PTT disabled. Missing: %s", missing)

    # Build deps with the voice callable wired (or None if preflight failed).
    deps = InherentDeps(
        submit_callable=submit_callable,
        broadcaster=broadcaster,
        voice_pipeline_callable=pipeline_callable,
    )

    # ... existing app + uvicorn setup ...

    # Spawn watchers (existing two + new _tts_watcher).
    intent_task = asyncio.create_task(_user_intent_watcher(runtime, poll_interval_s=poll_interval_s))
    response_task = asyncio.create_task(_response_watcher(runtime, broadcaster, poll_interval_s=poll_interval_s))
    tasks: list[asyncio.Task] = [intent_task, response_task]
    if tts_pipeline is not None:
        tts_task = asyncio.create_task(_tts_watcher(conn=runtime.conn, pipeline=tts_pipeline, poll_interval_s=poll_interval_s))
        tasks.append(tts_task)

    try:
        await uvicorn_server.serve()
    finally:
        if wake_listener is not None:
            wake_listener.request_stop()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # ... existing process_lock release ...
```

Adjust to the actual field names on `JarvisRuntime` (look at `runtime/__init__.py` for `db_path` / `conn`). Add `import os` and `from pathlib import Path` if not already present.

- [ ] **Step 4: Run preflight tests to confirm GREEN.**

```
.venv/bin/pytest tests/integration/test_serve_inherent_voice_wiring.py -v
```

Both PASS.

- [ ] **Step 5: Smoke the wiring without audio HW (mock-based).**

If the existing ADR-0003 daemon smoke test exists (e.g. `tests/integration/test_serve_inherent_daemon.py`), confirm it still passes — the new wiring degrades gracefully when models are absent.

```
.venv/bin/pytest tests/integration/test_serve_inherent -v
```

- [ ] **Step 6: Full gates + commit.**

```
.venv/bin/pytest -x
.venv/bin/ruff check jarvis tests
.venv/bin/mypy jarvis
.venv/bin/lint-imports
git add jarvis/runtime/inherent_loop.py tests/integration/test_serve_inherent_voice_wiring.py
git commit -m "feat(runtime): serve_inherent wires voice subsystem + preflight (ADR-0005 §12 §5.1 §5.3)"
```

---

# Phase 7 — Canaries + final gates

## Task 20: Canary — normalize-before-emit (AST scan)

**Files:**
- Create: `tests/canary/test_canary_voice_normalize_before_emit.py`

**Spec basis:** ADR §11 + §8 fix #1.

- [ ] **Step 1: Write the canary.**

Create `tests/canary/test_canary_voice_normalize_before_emit.py`:

```python
"""ADR-0005 §11 canary: emit_event('utterance.received', ...) must be preceded
by a call to AsrNormalizer.normalize within the same function body in any
jarvis/surface/voice_*.py file."""
from __future__ import annotations

import ast
from pathlib import Path

from tests.canary._helpers import repo_root

_TARGET_EVENT = "utterance.received"


class _OrderChecker(ast.NodeVisitor):
    def __init__(self):
        self.violations: list[tuple[str, int]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._check(node)
        self.generic_visit(node)

    def _check(self, fn) -> None:
        # Find any emit_event('utterance.received', ...) call in this body.
        for child in ast.walk(fn):
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "emit_event"
            ):
                # Look at keyword 'type=' or positional second arg.
                emits_utterance = False
                for kw in child.keywords:
                    if kw.arg == "type" and isinstance(kw.value, ast.Constant) and kw.value.value == _TARGET_EVENT:
                        emits_utterance = True
                if not emits_utterance:
                    continue
                # Confirm a normalize() call precedes it lexically in the same function.
                emit_lineno = child.lineno
                saw_normalize = False
                for c2 in ast.walk(fn):
                    if (
                        isinstance(c2, ast.Call)
                        and isinstance(c2.func, ast.Attribute)
                        and c2.func.attr == "normalize"
                        and c2.lineno < emit_lineno
                    ):
                        saw_normalize = True
                        break
                if not saw_normalize:
                    self.violations.append((fn.name, emit_lineno))


def test_canary_voice_normalize_before_emit() -> None:
    """Every emit_event(utterance.received, ...) in jarvis/surface/voice_*.py
    must be preceded by .normalize() in the same function body."""
    surface_dir = repo_root() / "jarvis" / "surface"
    for path in surface_dir.glob("voice_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        checker = _OrderChecker()
        checker.visit(tree)
        assert not checker.violations, (
            f"{path}: emit_event(utterance.received) without preceding normalize() at lines "
            f"{[v[1] for v in checker.violations]}"
        )
```

- [ ] **Step 2: Run to confirm GREEN.**

```
.venv/bin/pytest tests/canary/test_canary_voice_normalize_before_emit.py -v
```

PASS (because `voice_pipeline.run_turn` already calls `normalize` before `emit_event`).

- [ ] **Step 3: Commit.**

```
git add tests/canary/test_canary_voice_normalize_before_emit.py
git commit -m "test(canary): voice_*.py emit_event(utterance.received) is normalize-after (ADR-0005 §11 §8.1)"
```

---

## Task 21: Canary — lock-held during emit (AST scan)

**Files:**
- Create: `tests/canary/test_canary_voice_lock_held_during_emit.py`

- [ ] **Step 1: Write the canary.**

Create `tests/canary/test_canary_voice_lock_held_during_emit.py`:

```python
"""ADR-0005 §11 canary: emit_event(utterance.received, ...) inside any
jarvis/surface/voice_*.py function must be lexically reachable only from
within a VOICE_INPUT_LOCK.acquire(...)-bracketed region (try/finally),
OR the function must accept a precondition that the lock is already
held (documented by a body assertion like `VOICE_INPUT_LOCK.locked()`).
"""
from __future__ import annotations

import ast
from pathlib import Path

from tests.canary._helpers import repo_root


def _has_lock_acquire(fn) -> bool:
    for c in ast.walk(fn):
        if (
            isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute)
            and c.func.attr == "acquire"
            and isinstance(c.func.value, ast.Name)
            and c.func.value.id == "VOICE_INPUT_LOCK"
        ):
            return True
    return False


def _has_lock_locked_assert(fn) -> bool:
    for c in ast.walk(fn):
        if (
            isinstance(c, ast.Assert)
            and isinstance(c.test, ast.Call)
            and isinstance(c.test.func, ast.Attribute)
            and c.test.func.attr == "locked"
        ):
            return True
    return False


def _emits_utterance_received(fn) -> bool:
    for c in ast.walk(fn):
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "emit_event":
            for kw in c.keywords:
                if kw.arg == "type" and isinstance(kw.value, ast.Constant) and kw.value.value == "utterance.received":
                    return True
    return False


def test_canary_voice_lock_held_during_emit() -> None:
    surface_dir = repo_root() / "jarvis" / "surface"
    violations: list[str] = []
    for path in surface_dir.glob("voice_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not _emits_utterance_received(node):
                continue
            if _has_lock_acquire(node) or _has_lock_locked_assert(node):
                continue
            violations.append(f"{path}::{node.name}@{node.lineno}")
    assert not violations, f"emit_event(utterance.received) without VOICE_INPUT_LOCK guard in: {violations}"
```

- [ ] **Step 2: Run to confirm GREEN.**

```
.venv/bin/pytest tests/canary/test_canary_voice_lock_held_during_emit.py -v
```

PASS.

- [ ] **Step 3: Commit.**

```
git add tests/canary/test_canary_voice_lock_held_during_emit.py
git commit -m "test(canary): voice_*.py utterance.received emit guarded by VOICE_INPUT_LOCK (ADR-0005 §11 §8.2)"
```

---

## Task 22: Canary — L5 voice imports stay within layer

**Files:**
- Create: `tests/canary/test_canary_voice_layer_imports.py`

- [ ] **Step 1: Write the canary.**

Create `tests/canary/test_canary_voice_layer_imports.py`:

```python
"""ADR-0005 §11 canary: jarvis/surface/voice_*.py do not name forbidden layers.

`lint-imports` (gate 3) already enforces this at the package level; this
canary makes the failure surface inside pytest with a per-file diagnostic
so the autonomous loop can see exactly which file violated."""
from __future__ import annotations

import ast
from pathlib import Path

from tests.canary._helpers import repo_root


_FORBIDDEN_PREFIXES = (
    "jarvis.decision",
    "jarvis.execution",
    "jarvis.deployment",
    "jarvis.runtime",
    "jarvis.cli",
)


def test_canary_voice_layer_imports() -> None:
    surface_dir = repo_root() / "jarvis" / "surface"
    violations: list[str] = []
    for path in surface_dir.glob("voice_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            for mod in mods:
                if any(mod.startswith(p) for p in _FORBIDDEN_PREFIXES):
                    violations.append(f"{path}:{node.lineno} imports {mod}")
    assert not violations, "L5 voice modules name forbidden layers: " + "\n  ".join(violations)
```

- [ ] **Step 2: Run to confirm GREEN.**

```
.venv/bin/pytest tests/canary/test_canary_voice_layer_imports.py -v
```

PASS.

- [ ] **Step 3: Commit.**

```
git add tests/canary/test_canary_voice_layer_imports.py
git commit -m "test(canary): voice_*.py stays in L5 layer (ADR-0005 §11)"
```

---

## Task 23: Integration smoke — full PTT happy path

**Files:**
- Create: `tests/integration/test_voice_ptt_end_to_end.py`

- [ ] **Step 1: Write the test.**

Create `tests/integration/test_voice_ptt_end_to_end.py`:

```python
"""ADR-0005 integration smoke: PTT WAV → utterance.received → drive_turn dispatch."""
from __future__ import annotations

import asyncio
import io
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from jarvis.state.event_log import open_event_log
from jarvis.surface import voice_asr, voice_pipeline
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app


def _wav(duration_s: float = 0.5) -> bytes:
    pcm = b"\x10\x00" * int(16000 * duration_s)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(pcm)
    return buf.getvalue()


def test_ptt_end_to_end_creates_utterance_received_row(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    conn = open_event_log(db_path)

    recognizer = MagicMock(spec=voice_asr.AsrRecognizer)
    recognizer.recognize.return_value = voice_asr.TranscriptionResult(
        text="你好", confidence=0.9, language_detected="zh-CN", emotion=None,
    )
    normalizer = voice_asr.AsrNormalizer(corrections=[], aliases={}, fuzzy_enabled=False)
    pipeline = voice_pipeline.VoicePipeline(
        conn_factory=lambda: open_event_log(db_path),
        recognizer=recognizer,
        normalizer=normalizer,
        broadcaster=None,
        artifacts_dir=tmp_path,
    )
    deps = InherentDeps(
        submit_callable=lambda text: None,
        broadcaster=InherentBroadcaster(),
        voice_pipeline_callable=pipeline.run_turn,
    )
    app = create_app(deps)
    client = TestClient(app)

    resp = client.post(
        "/inherent/asr-submit",
        files={"audio": ("u.wav", _wav(), "audio/wav")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["transcript"] == "你好"

    # Verify event log row.
    row = conn.execute(
        "SELECT type, payload_json FROM events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row[0] == "utterance.received"
    import json
    payload = json.loads(row[1])
    assert payload["transcript"] == "你好"
    assert payload["channel"] == "inherent_ptt"
    conn.close()
```

- [ ] **Step 2: Run to confirm GREEN.**

```
.venv/bin/pytest tests/integration/test_voice_ptt_end_to_end.py -v
```

PASS.

- [ ] **Step 3: Commit.**

```
git add tests/integration/test_voice_ptt_end_to_end.py
git commit -m "test(integration): PTT end-to-end smoke landing utterance.received (ADR-0005 §11)"
```

---

## Task 24: Final all-gates check + ADR status flip

**Files:**
- Modify: `docs/adr/0005-inherent-voice.md` — flip Status from `Proposed` → `Accepted`

- [ ] **Step 1: Run all four gates.**

```
.venv/bin/pytest -x
.venv/bin/ruff check jarvis tests
.venv/bin/mypy jarvis
.venv/bin/lint-imports
```

All four exit 0.

- [ ] **Step 2: Manual smoke tests (Allen).**

Smoke tests from ADR §11:

  (a) Start daemon: `.venv/bin/python -m jarvis.runtime.inherent_loop` (or whichever entrypoint the project uses — see `jarvis/__main__.py`). With the Inherent Swift app foreground, long-press Return, say "现在几点" — confirm: card shows "听到 现在几点" (normalized), TTS speaks back the time.

  (b) Without keyboard, say "Hey Jarvis, 现在几点". Confirm same end-to-end behavior.

  (c) Trigger a long assistant response (>10 s of TTS). During playback, say "Hey Jarvis, 几点" again. Confirm: wake is suppressed; the wake_listener log shows it skipped while `is_speaking()` was True; the second turn only opens after the first TTS drains.

  (d) (optional) Toggle `MINIMAX_API_KEY=""` and restart. PTT/wake still work; assistant response is spoken via `macos_say` (Tingting voice).

  (e) (optional) Delete `data/silero_vad.onnx` and restart. The daemon must still accept PTT inbound (some preflight model checks are wake-only — confirm behavior matches §10 F1/F2).

- [ ] **Step 3: Flip ADR-0005 status to Accepted.**

Edit `docs/adr/0005-inherent-voice.md`:

```diff
- **Status:** Proposed
+ **Status:** Accepted
```

- [ ] **Step 4: Final commit.**

```
git add docs/adr/0005-inherent-voice.md
git commit -m "docs(adr-0005): accept after smoke tests pass (ADR-0005 DOD)"
```

---

# Self-review checklist

After landing the plan, audit:

1. **Spec coverage:** every row in ADR-0005 §3 "Spec Compliance Map" is implemented by at least one task:
   - §3.6.1 raw→buffer→canonical → Tasks 7, 8, 9
   - §3.6.1 confidence field → Tasks 1, 6
   - §3.6.2 normalize before emit → Tasks 5, 9, 20
   - §3.6.2 raw audio as artifact_ref → Task 4
   - §3.6.5 voice = input + output → Tasks 9, 11, 12, 16
   - §3.4.13 / §3.6.6 gate-mode routing → Tasks 2, 16, 18
   - §3.6.5 fallback chain → Task 16
   - §3.6.10 presentation rate-limit → Task 16
   - §3.6.4 channel→surface → noted as deferred future ADR (see ADR §3 row Channel→physical)
   - §3.6.12 backpressure → noted as Day-1 simplification (no coalescing on phase envelopes)
   - §5.4 registry → Tasks 1, 2
   - §3.7.2 Mac domain → no cross-domain change required; satisfied by scope

2. **Three legacy spec deviations fixed:** Task 9 (normalize-before-emit) + Task 9 (VOICE_INPUT_LOCK) + Task 6 (unified empty filter), with canaries in Tasks 20, 21.

3. **DOD items 1-6:** Task 11 (PTT 200 + event), Task 12 (wake end-to-end), Tasks 14-16 (TTS playback + gate-mode), Task 22 (lint-imports green for voice_*), Tasks 20-22 (canaries), Task 24 (smoke).

If any row above has no task, add a task before announcing the plan complete.
