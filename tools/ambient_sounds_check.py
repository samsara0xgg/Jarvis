"""Acceptance check for ambient sounds (ADR 0151).

    .venv/bin/python tools/ambient_sounds_check.py [--audio path.wav]

1. Scenario: helper-format JSON lines through the filter and the line producer on a fake clock
   (playback overlap and tail, thresholds, whitelist, family max, debounce, shown-once, 200 chars).
2. Real run: builds ``native/ambient_sounds``, pipes 16 kHz audio (--audio, else a generated
   speech-plus-tone clip) through ``AmbientSounds.feed``, prints the helper's raw lines, kills the
   helper, and waits for the supervisor to restart it.
"""

# ruff: noqa: T201, S101, S603, S607, PLR2004, D103, E501, PLR0915, PT018

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

from jarvis.surface import ambient_sounds as amb
from jarvis.surface.voice_native_out import ensure_helper_binary

RATE = 16_000


def _line(t: float, **labels: float) -> str:
    return json.dumps({"t": t, "labels": [[k, v] for k, v in labels.items()]})


def scenario() -> None:
    clock = [1000.0]
    log = Path(tempfile.mkdtemp(prefix="ambient-log-")) / "ambient-sounds.jsonl"
    s = amb.AmbientSounds(clock=lambda: clock[0], log_path=log)

    def at(t: float) -> None:  # advance audio and wall clock to second t of the run
        clock[0] = 1000.0 + t

    s.track(30 * RATE, speaking=False)  # 0-30 s: quiet
    s.track(2 * RATE, speaking=True)  # 30-32 s: she speaks
    s.track(10 * RATE, speaking=False)  # 32-42 s

    at(20)
    s.ingest(_line(20, cough=0.97, speech=0.6))  # counts
    at(33)
    s.ingest(_line(33, sigh=0.99))  # window 30-33 holds her voice: dropped
    at(34.4)
    s.ingest(_line(34.4, sigh=0.99))  # window starts 31.4, she stopped at 32: dropped
    at(35.4)
    s.ingest(_line(35.4, sigh=0.99))  # window 32.4-35.4, tail ends 32.5: dropped
    at(35.6)
    s.ingest(_line(35.6, sigh=0.99))  # window starts 32.6, clear of the tail: counts
    at(36)
    s.ingest(_line(36, cough=0.89, door=0.85, door_slam=0.5, breathing=0.99, drawer=0.95))  # none
    at(37)
    s.ingest(_line(37, bird_chirp_tweet=0.9, bird=0.4, music=0.5, piano=0.93, typing=0.2))  # bird, music
    at(38)
    s.ingest(_line(38, chuckle_chortle=0.5, laughter=0.4, snicker=0.3))  # family max 0.5: none
    at(39)
    s.ingest(_line(39, giggling=0.92, laughter=0.3))  # laughter
    at(40)
    s.ingest("not json")
    s.ingest(json.dumps({"t": 1}))

    got = {e.name for e in s._events}  # noqa: SLF001
    assert got == {"cough", "sigh", "bird", "music", "laughter"}, got
    line = s.line()
    print(line)
    assert line == "Sounds around him (background, rarely worth mentioning; last 10 min, from audio): laughed, coughed, sighed, birds, music on.", line
    assert s.line() == line  # every turn, not only once
    rows = [json.loads(r) for r in log.read_text().splitlines()]
    assert len(rows) == 9 and [r["her_voice"] for r in rows[:4]] == [False, True, True, True], rows
    assert rows[0]["kept"] == ["cough"] and rows[5]["kept"] == [], rows  # labels logged, kept named

    # Debounce: same label within 10 s is one event; 10 s of quiet makes the next one new.
    at(100)
    for t in (100, 105, 109):
        at(t)
        s.ingest(_line(t, cough=0.95))
    at(125)
    s.ingest(_line(125, cough=0.95))
    at(126)
    line = s.line()
    print(line)
    assert line is not None and "coughed repeatedly" in line, line

    # A new sound joins the ones still inside the 10 minutes.
    at(130)
    s.ingest(_line(130, knock=0.95))
    s.ingest(_line(130, knock=0.95))
    at(131)
    assert "knocking" in (s.line() or ""), "knock"

    # Uncertain laughter: two consecutive windows at 0.4-0.9 is a guess; one, or a gap, is nothing.
    clock[0] += 700  # everything above is now older than 10 minutes
    s.ingest(_line(200, laughter=0.45))
    s.ingest(_line(204, laughter=0.5))  # 4 s later: not consecutive
    assert s.line() is None, "lone windows"
    s.ingest(_line(205.5, giggling=0.55))  # consecutive with 204
    s.ingest(_line(207, cough=0.95, music=0.9))
    line = s.line()
    print(line)
    assert line == "Sounds around him (background, rarely worth mentioning; last 10 min, from audio): coughed, music on; maybe laughed (~55%, this detector under-scores his laugh).", line
    clock[0] += 30
    s.ingest(_line(300, laughter=0.5))
    s.ingest(_line(301.5, laughter=0.6))
    s.ingest(_line(303, laughter=0.95))  # a sure laugh wins over the guess
    sure_line = s.line() or ""
    assert "laughed" in sure_line and "maybe laughed" not in sure_line, sure_line

    # The 200-character cap holds with every name present, dropping whole phrases.
    clock[0] += 400
    s.track(10 * RATE, speaking=False)
    for labels in amb._FAMILIES.values():  # noqa: SLF001
        for i in range(2):
            clock[0] += amb._DEBOUNCE_S + 1  # noqa: SLF001
            s.ingest(_line(1000 + i, **{labels[0]: 0.95}))
    full = s.line()
    print(full)
    assert full is not None and len(full) <= 200 and full.endswith("."), full


def real_run(audio: Path | None) -> None:
    binary = ensure_helper_binary(amb._HELPER_DIR, "jarvis-ambient-sounds")  # noqa: SLF001
    print("helper:", binary)
    tmp = Path(tempfile.mkdtemp(prefix="ambient-check-"))
    if audio is None:
        aiff = tmp / "c.aiff"
        subprocess.run(["say", "-o", str(aiff), "-r", "120", "I have a cold and I keep sneezing and coughing today."], check=True)
        audio = tmp / "c.wav"
        subprocess.run(["afconvert", str(aiff), str(audio), "-d", "LEI16@16000", "-c", "1", "-f", "WAVE"], check=True)
    with wave.open(str(audio)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (RATE, 1, 2), "need 16 kHz mono int16"
        pcm = w.readframes(w.getnframes()) * 3
    # Raw helper output.
    out = subprocess.run([str(binary)], input=pcm, capture_output=True, check=True, timeout=60).stdout
    lines = [json.loads(x) for x in out.splitlines()]
    print(f"{len(pcm) / 2 / RATE:.1f} s in, {len(lines)} result lines out")
    for x in lines[:5]:
        print("  ", x)
    assert all("t" in x and x["labels"] for x in lines)
    assert not any(lab[0] in ("speech", "silence") for x in lines for lab in x["labels"])

    # Through the class: feed, kill the helper, see the supervisor bring a new one up.
    s = amb.AmbientSounds()
    s.start()
    deadline = time.monotonic() + 120
    while s._queue is None and time.monotonic() < deadline:  # noqa: SLF001
        time.sleep(0.1)
    assert s._queue is not None, "helper never came up"  # noqa: SLF001
    for i in range(0, min(len(pcm), 16 * RATE), 1024):
        s.feed(pcm[i : i + 1024], speaking=False)
    time.sleep(1.0)
    first = s._proc  # noqa: SLF001
    assert first is not None and first.poll() is None
    first.kill()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and (s._proc is None or s._proc is first or s._queue is None):  # noqa: SLF001
        time.sleep(0.1)
    assert s._proc is not None and s._proc is not first and s._proc.poll() is None, "no restart"  # noqa: SLF001
    print("killed helper pid", first.pid, "-> restarted as pid", s._proc.pid)  # noqa: SLF001
    s.stop()
    assert s._thread is not None and not s._thread.is_alive()  # noqa: SLF001
    print("stopped cleanly")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", type=Path)
    ns = ap.parse_args()
    scenario()
    print("scenario OK")
    real_run(ns.audio)
    print("real run OK")
    sys.exit(0)
