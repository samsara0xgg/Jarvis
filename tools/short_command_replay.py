"""Replay short recordings with and without the second hearing of ADR 0137.

    /Users/alllllenshi/Projects/jarvis/.venv/bin/python tools/short_command_replay.py ROWS.json

``ROWS.json`` is a list of rows with ``audio`` (an m4a path) and ``turn_id``. Each clip is
decoded (afconvert, 16 kHz mono PCM16) and heard by the real ``HybridFinalRecognizer`` twice,
without and with ``whisper_command``, its length standing for its speech as in ``recognize``.
Prints how often the second hearing fired, each change, and the extra milliseconds it cost.
Needs the models under ``~/.jarvis/models`` and mlx-whisper (not in this checkout's venv).
"""

# ruff: noqa: T201, S603, S607 - a report printed to the terminal; afconvert is a fixed system tool

from __future__ import annotations

import json
import statistics
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # the venv's editable install may point at another checkout
    sys.path.insert(0, str(REPO_ROOT))

from jarvis.runtime.dictation import COMMAND_PROMPT, whisper_ears  # noqa: E402
from jarvis.surface import voice_asr  # noqa: E402

MODELS = Path.home() / ".jarvis/models"


def decode(path: Path, tmp: Path) -> bytes:
    """One recording as 16 kHz mono PCM16."""
    wav = tmp / f"{path.stem}.wav"
    subprocess.run(
        ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(path), str(wav)],
        check=True,
    )
    with wave.open(str(wav)) as reader:
        pcm: bytes = reader.readframes(reader.getnframes())
    return pcm


def main() -> None:
    """Hear every clip both ways and print the report."""
    rows = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    sensevoice = voice_asr.SenseVoiceRecognizer(model_dir=MODELS / "sensevoice-small-int8")
    zh, en = whisper_ears(language="zh"), whisper_ears(language="en")
    command = whisper_ears(language="zh", prompt=COMMAND_PROMPT)
    if zh is None or en is None or command is None:
        sys.exit("mlx-whisper is not installed")
    spent: list[float] = []
    inner = command.recognize

    def timed(audio: bytes) -> voice_asr.TranscriptionResult:
        started = time.perf_counter()
        try:
            return inner(audio)
        finally:
            spent.append((time.perf_counter() - started) * 1000)

    command.recognize = timed  # type: ignore[method-assign, assignment]
    plain = voice_asr.HybridFinalRecognizer(sensevoice=sensevoice, whisper_zh=zh, whisper_en=en)
    second = voice_asr.HybridFinalRecognizer(
        sensevoice=sensevoice, whisper_zh=zh, whisper_en=en, whisper_command=command,
    )
    plain.prewarm()
    command.prewarm()
    fired: list[float] = []
    changes: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        for row in rows:
            pcm = decode(Path(row["audio"]), Path(tmp))
            seconds = len(pcm) / 2 / 16_000
            before = plain.recognize(pcm).text
            spent.clear()
            after = second.recognize(pcm).text
            if spent:
                fired.append(spent[0])
            if after != before:
                changes.append(f"{row['turn_id']}: {before} -> {after}")
            print(f"{row['turn_id']} {seconds:.1f}s fired={bool(spent)}", flush=True)
    print(f"\nclips: {len(rows)}; second hearing fired: {len(fired)}; changed: {len(changes)}")
    for change in changes:
        print(f"  {change}")
    if fired:
        median = statistics.median(fired)
        print(f"extra ms where it fired: median {median:.0f}, max {max(fired):.0f}")


if __name__ == "__main__":
    main()
