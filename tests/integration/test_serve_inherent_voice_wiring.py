"""ADR-0005 §12: serve_inherent skips wake when models missing; wires voice pipeline always."""
from __future__ import annotations

from typing import TYPE_CHECKING

from jarvis.runtime import inherent_loop

if TYPE_CHECKING:
    from pathlib import Path


def test_preflight_model_check_returns_false_when_missing(tmp_path: Path) -> None:
    """Pre-flight returns False with a missing-models list."""
    ok, missing = inherent_loop._voice_models_preflight(  # noqa: SLF001 — preflight is module-private by design.
        sensevoice_dir=tmp_path / "missing-sv",
        silero_path=tmp_path / "missing-silero.onnx",
    )
    assert ok is False
    # Either sensevoice or silero should be in the missing list.
    joined = " ".join(missing).lower()
    assert "sensevoice" in joined or "silero" in joined


def test_preflight_model_check_returns_true_when_present(tmp_path: Path) -> None:
    """Pre-flight returns True when both SenseVoice + Silero assets are on disk."""
    sv = tmp_path / "sv"
    sv.mkdir()
    (sv / "model.int8.onnx").write_bytes(b"x")
    (sv / "tokens.txt").write_text("")
    silero = tmp_path / "silero_vad.onnx"
    silero.write_bytes(b"x")
    ok, missing = inherent_loop._voice_models_preflight(  # noqa: SLF001 — preflight is module-private by design.
        sensevoice_dir=sv,
        silero_path=silero,
    )
    assert ok is True
    assert missing == []
