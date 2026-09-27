"""The speech models the voice stack needs, fetched once into the runtime root.

SenseVoice (int8 only, 239 MB) and Silero VAD come from the sherpa-onnx
author's public releases; each file is checked against its SHA-256 before it
is moved into place, so a truncated or tampered download never loads.
Stdlib only, like the rest of the deployment layer's path handling.
"""

from __future__ import annotations

import hashlib
import logging
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pathlib import Path

LOGGER = logging.getLogger(__name__)

_SENSEVOICE_URL: Final = (
    "https://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
    "/resolve/main/"
)
_TIMEOUT_S: Final = 60.0
_CHUNK: Final = 1 << 20


@dataclass(frozen=True)
class ModelFile:
    """One file the voice stack loads: where it comes from, its size and what it must hash to."""

    url: str
    sha256: str
    size: int


@dataclass
class Progress:
    """The first-boot fetch as the desktop sees it (``GET /inherent/setup``)."""

    done: int = 0
    total: int = 0
    failed: bool = False

    def view(self) -> dict[str, object]:
        """``ready`` when nothing had to be fetched this boot."""
        state = "failed" if self.failed else "downloading" if self.total else "ready"
        return {"state": state, "done": self.done, "total": self.total}


SENSEVOICE_FILES: Final[dict[str, ModelFile]] = {
    "model.int8.onnx": ModelFile(
        _SENSEVOICE_URL + "model.int8.onnx",
        "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51",
        239_233_841,
    ),
    "tokens.txt": ModelFile(
        _SENSEVOICE_URL + "tokens.txt",
        "f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc",
        315_894,
    ),
}
SILERO_VAD: Final = ModelFile(
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx",
    "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6",
    643_854,
)


def default_sensevoice_dir(root: Path) -> Path:
    """``<root>/models/sensevoice-small-int8``: where a fresh install keeps SenseVoice."""
    return root / "models" / "sensevoice-small-int8"


def default_silero_vad_path(root: Path) -> Path:
    """``<root>/models/silero_vad.onnx``: where a fresh install keeps Silero VAD."""
    return root / "models" / "silero_vad.onnx"


def missing_models(sensevoice_dir: Path, silero_vad_path: Path) -> dict[Path, ModelFile]:
    """The model files not yet on disk, by the path each belongs at."""
    wanted = {sensevoice_dir / name: f for name, f in SENSEVOICE_FILES.items()}
    wanted[silero_vad_path] = SILERO_VAD
    return {path: f for path, f in wanted.items() if not path.exists()}


def download(path: Path, model: ModelFile, progress: Progress | None = None) -> None:
    """Fetch ``model`` to ``path``; a hash mismatch raises and leaves nothing behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    digest = hashlib.sha256()
    try:
        request = urllib.request.urlopen(model.url, timeout=_TIMEOUT_S)  # noqa: S310 — fixed https URLs
        with request as response, part.open("wb") as out:
            while chunk := response.read(_CHUNK):
                digest.update(chunk)
                out.write(chunk)
                if progress is not None:
                    progress.done += len(chunk)
        if digest.hexdigest() != model.sha256:
            msg = f"{model.url}: sha256 {digest.hexdigest()} is not the expected {model.sha256}"
            raise ValueError(msg)
        part.replace(path)
    finally:
        part.unlink(missing_ok=True)


def fetch_missing(sensevoice_dir: Path, silero_vad_path: Path, progress: Progress) -> None:
    """Download every missing model file, counting bytes into ``progress``; a failure raises."""
    missing = missing_models(sensevoice_dir, silero_vad_path)
    progress.total = sum(model.size for model in missing.values())
    for path, model in missing.items():
        LOGGER.info("models: downloading %s", model.url)
        download(path, model, progress)
        LOGGER.info("models: %s ready", path)
