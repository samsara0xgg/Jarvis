"""A fresh runtime root gets its speech models by download, and only verified bytes land.

Serves stand-in files from a local HTTP server and drives the real downloader:
a missing model is fetched into place; a download whose SHA-256 does not match
raises and leaves neither the file nor a partial behind, so the daemon's
preflight still reports it missing instead of loading a bad model.
"""

from __future__ import annotations

import functools
import hashlib
import http.server
import threading
from typing import TYPE_CHECKING

import pytest

from jarvis.deployment.models import (
    ModelFile,
    default_sensevoice_dir,
    default_silero_vad_path,
    missing_models,
)
from jarvis.deployment.models import download as real_download

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[str, bytes]]:
    """A local server with one stand-in model file; yields its URL and bytes."""
    body = b"not really a model"
    (tmp_path / "srv").mkdir()
    (tmp_path / "srv" / "model.onnx").write_bytes(body)
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(tmp_path / "srv")
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/model.onnx", body
    finally:
        server.shutdown()


def test_a_fresh_root_lists_every_model_as_missing(tmp_path: Path) -> None:
    """An empty root needs all three files, each at its place under models/."""
    root = tmp_path / "jarvis"
    missing = missing_models(default_sensevoice_dir(root), default_silero_vad_path(root))
    assert sorted(p.relative_to(root).as_posix() for p in missing) == [
        "models/sensevoice-small-int8/model.int8.onnx",
        "models/sensevoice-small-int8/tokens.txt",
        "models/silero_vad.onnx",
    ]


def test_a_verified_download_lands_in_place(tmp_path: Path, served: tuple[str, bytes]) -> None:
    """Matching bytes are moved into place; no partial file stays beside them."""
    url, body = served
    target = tmp_path / "jarvis" / "models" / "silero_vad.onnx"
    real_download(target, ModelFile(url, hashlib.sha256(body).hexdigest()))
    assert target.read_bytes() == body
    assert list(target.parent.iterdir()) == [target]


def test_a_hash_mismatch_leaves_nothing_behind(tmp_path: Path, served: tuple[str, bytes]) -> None:
    """Wrong bytes raise, and neither the model nor its partial is left on disk."""
    url, _ = served
    target = tmp_path / "jarvis" / "models" / "silero_vad.onnx"
    with pytest.raises(ValueError, match="sha256"):
        real_download(target, ModelFile(url, "0" * 64))
    assert list(target.parent.iterdir()) == []
