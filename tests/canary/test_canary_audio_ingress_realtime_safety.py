"""Static Wave-3 guards for ADC callback safety and scope containment."""

# ruff: noqa: SLF001 - private production seams are the canary target.

from __future__ import annotations

import ast
import inspect
import textwrap
from typing import TYPE_CHECKING

from jarvis.runtime import inherent_loop
from jarvis.surface import voice_audio, voice_backend, voice_session

if TYPE_CHECKING:
    from collections.abc import Iterator


def _functions(tree: ast.AST, name: str) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            yield node


def _call_name(call: ast.Call) -> str:
    target = call.func
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return ""


def test_portaudio_input_callback_contains_no_blocking_or_side_effect_boundary() -> None:
    """The registered ADC callback may only do scalar work + private frame sink."""
    tree = ast.parse(
        textwrap.dedent(inspect.getsource(voice_backend.SoundDeviceDuplexBackend.start)),
    )
    callbacks = list(_functions(tree, "_callback"))
    assert len(callbacks) == 1
    callback = callbacks[0]
    assert not any(isinstance(node, ast.Await) for node in ast.walk(callback))
    forbidden = {
        "acquire",
        "append",
        "emit_event",
        "execute",
        "info",
        "log",
        "open",
        "print",
        "put",
        "record_realtime_trace",
        "sleep",
        "submit",
        "wait",
        "warning",
    }
    calls = {_call_name(node) for node in ast.walk(callback) if isinstance(node, ast.Call)}
    assert not calls.intersection(forbidden)
    assert "frame_sink" in calls


def test_realtime_input_production_path_is_callback_only_and_has_no_stream_read() -> None:
    """Wave-3 wiring cannot regress to wake/capture ``stream.read()``."""
    sources = "\n".join(
        (
            inspect.getsource(voice_backend.SoundDeviceDuplexBackend),
            inspect.getsource(voice_audio.AudioIngress),
            inspect.getsource(voice_session.DuplexVoiceSession),
            inspect.getsource(inherent_loop._spawn_single_ingress_session),
        ),
    )
    assert ".read(" not in inspect.getsource(voice_backend.SoundDeviceDuplexBackend)
    assert "RawInputStream" not in inspect.getsource(voice_session.DuplexVoiceSession)
    assert "_open_input_stream" not in sources
    assert "_open_wake_input_stream" not in sources
    backend_module_source = inspect.getsource(voice_backend)
    assert "callback=callback" in backend_module_source
    assert "RawInputStream" in backend_module_source


def test_wave3_input_session_has_no_hard_cancel_or_system_mute_authority() -> None:
    """Step 5 cannot cross into ADR-0006 Step 6 or reintroduce capture mute."""
    source = inspect.getsource(voice_session)
    forbidden = {
        "ActionRunner",
        "ResponseCancelRequest",
        "SystemAudioDucker",
        "interrupt_generation(",
        "interrupt_playback(",
        ".flush(",
        ".duck(",
    }
    assert not {token for token in forbidden if token in source}
    assert "hard_cancel_performed=False" in source
    assert "wave3_no_interrupt_policy_during_output" in source
