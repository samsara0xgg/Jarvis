"""Cross-language check of the /phone/ws wire (ADR 0209): the host's own code against the Swift codec.

    host_codec_check.py export DIR HOST_SRC   host encodes frames into DIR (the Swift harness decodes them)
    host_codec_check.py verify DIR HOST_SRC   host decodes the frames the Swift harness wrote into DIR

HOST_SRC holds the host's `jarvis/` package exported from origin/main (run.sh does that with `git archive`); the
jarvis venv's python runs this. Frames go through the real serve_phone route and PhoneAudioStreamPlayer, with a fake socket.
"""

import asyncio
import json
import logging
import sqlite3
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

logging.disable(logging.CRITICAL)  # the retry case logs the failure it provokes
mode, out, host_src = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
sys.path.insert(0, host_src)
from jarvis.surface import phone_link, phone_player  # noqa: E402

assert phone_link.__file__.startswith(host_src), phone_link.__file__
READY_BODY = struct.pack("<IIqq", 32_000, 512, 20_000_000, 123_456_789_012)


class FakeWs:
    """Scripted frames in, everything the host sends out; then a disconnect."""

    def __init__(self, script):
        self.script, self.texts, self.binaries = list(script), [], []

    async def accept(self): ...
    async def close(self, code=1000): self.closed = code
    async def send_text(self, t): self.texts.append(json.loads(t))
    async def send_bytes(self, b): self.binaries.append(b)

    async def receive(self):
        if not self.script:
            await asyncio.sleep(0.3)  # let the row poller run
            return {"type": "websocket.disconnect"}
        f = self.script.pop(0)
        return {"type": "websocket.receive", "bytes" if isinstance(f, bytes) else "text": f}


def drive(script, *, voice: bool, fail_log: bool = False):
    """Run serve_phone over `script`; what the host sent, the calls it made and the player."""
    calls, sent = [], []
    player = phone_player.PhoneAudioStreamPlayer(send_binary=lambda b: sent.append(b) or True, generation_safe=True)
    reports = []
    real_write = player._callback_reports.write
    player._callback_reports.write = lambda **kw: (reports.append(kw), real_write(**kw))[1]

    class Voice:
        async def begin(self): calls.append(("begin",))
        async def stop_playback(self, reason):
            calls.append(("stop_playback", reason))
            return ""
        async def close(self): ...
    Voice.player = player

    async def open_voice(_device, _send): return Voice()

    class Cursor:
        n = 0
        def poll(self):
            Cursor.n += 1
            row = SimpleNamespace(type="surface.response_chunk", event_uid="a" * 32, ts_epoch_ms=1_700_000_000_000,
                                  payload={"turn_id": "T1", "text": "好的", "sequence": 0, "phase": "final"})
            return 1, ([(1, row)] if Cursor.n == 1 else [])

    def record_phone_say(device, uid, text, **kw):
        if fail_log:
            msg = "disk I/O error"
            raise sqlite3.OperationalError(msg)
        calls.append(("say", device, uid, text, kw))
        return "T" + uid

    events = SimpleNamespace(record_phone_say=record_phone_say)
    rows = SimpleNamespace(high_water=lambda: 0, cursor=lambda device, after, **kw: Cursor())
    hub = phone_link.PhoneHub(
        events=events, rows=rows, open_voice=open_voice if voice else None,
        barge_in=lambda device: calls.append(("barge_in", device)) or "ok",
        cancel_turn=lambda turn, reason: calls.append(("cancel_turn", turn, reason)) or "ok",
    )
    ws = FakeWs(script)
    asyncio.run(phone_link.serve_phone(hub, ws, "phone"))
    return ws.texts, ws.binaries, calls, player, reports, sent


def by(texts, kind, code=None):
    return next(t for t in texts if t["type"] == kind and (code is None or t.get("code") == code))


if mode == "export":
    hello = json.dumps({"type": "hello", "voice": True})
    say = json.dumps({"type": "say", "utterance_id": "u1", "text": "hi", "spoken": True})
    texts, *_ = drive([
        hello, bytes([0x81]) + READY_BODY, say, json.dumps({"type": "ping", "t_ns": 1234}),
        json.dumps({"type": "say"}), json.dumps({"type": "ping", "t_ns": "x"}),
    ], voice=True)
    for name, frame in {"ready_voice": by(texts, "ready"), "said": by(texts, "said"), "row": by(texts, "row"),
                        "pong": by(texts, "pong"), "error_bad_say": by(texts, "error", "bad_say"),
                        "error_bad_ping": by(texts, "error", "bad_ping")}.items():
        (out / f"{name}.json").write_text(json.dumps(frame, ensure_ascii=False))
    texts, *_ = drive([hello.replace("true", "false")], voice=False)
    (out / "ready_text.json").write_text(json.dumps(by(texts, "ready")))
    texts, *_ = drive([hello, say], voice=False, fail_log=True)
    (out / "error_retry.json").write_text(json.dumps(by(texts, "error", "retry")))
    # the binary frames come from the real player, as the media actor drives it
    sent = []
    player = phone_player.PhoneAudioStreamPlayer(send_binary=lambda b: sent.append(b) or True, generation_safe=True)
    player.on_binary(bytes([0x81]) + READY_BODY)  # frames flow only after the phone's READY
    player.send_frame(2, struct.pack("<q", 7))
    player._mirror.write(np.array([0, 1, -1, 16384, -32768, 1234], dtype=np.float32) / 32768.0, generation=5, output_start_cursor=96_000)
    player._mirror.request_discard()            # ACTIVE(-1) then DISCARD(1)
    player.set_gain(0.25, ramp_ms=60)           # 1920 samples at 32 kHz
    player.pause_generation(paused=True)
    names = ["active_7", "pcm", "active_none", "discard", "gain", "hold"]
    assert len(sent) == len(names), [s[0] for s in sent]
    for name, frame in zip(names, sent, strict=True):
        (out / f"{name}.bin").write_bytes(frame)
    print("host frames exported:", ", ".join(names))
else:
    rd = lambda n: (out / n).read_bytes()  # noqa: E731
    rt = lambda n: (out / n).read_text()  # noqa: E731
    script = [rt("swift_hello.json"), rd("swift_ready.bin"), rt("swift_say_spoken.json"), rt("swift_say_typed.json"), rt("swift_ping.json"),
              rd("swift_report.bin"), rd("swift_report_none.bin"), rd("swift_status.bin"), rd("swift_discard_ack.bin"),
              rt("swift_barge.json"), rt("swift_cancel.json")]
    texts, _, calls, player, reports, _ = drive(script, voice=True)
    assert not [t for t in texts if t["type"] == "error"], texts
    assert by(texts, "ready")["voice"] is True
    assert [t["turn_id"] for t in texts if t["type"] == "said"] == ["Tu1", "Tu2"], texts
    says = [c for c in calls if c[0] == "say"]
    assert says[0][1:4] == ("phone", "u1", "你好 Jarvis") and says[0][4] == {"spoken": True, "language": "zh", "confidence": 0.93}, says[0]
    assert says[1][1:4] == ("phone", "u2", "hi") and says[1][4]["spoken"] is False, says[1]
    assert by(texts, "pong")["t_ns"] == 1_234_567_890_123
    assert ("begin",) in calls and ("stop_playback", "barge_in") in calls and ("barge_in", "phone") in calls
    assert ("cancel_turn", "Tu2", "user_stop") in calls, calls
    # READY -> the player is open, with the device latency and a clock offset; the rate check would have raised on 48 kHz
    assert player._got_ready and player._estimated_output_latency_ns == 20_000_000 and player.clock_offset_ns is not None
    assert len(reports) == 2
    r = reports[0]
    assert (r["generation"], r["output_start_cursor"], r["output_end_cursor"], r["first_for_generation"]) == (9, 100, 740, True), r
    assert r["presentation_delay_ns"] == 66 and r["callback_monotonic_ns"] - 55 == player.clock_offset_ns, r
    assert reports[1]["generation"] == -1 and reports[1]["first_for_generation"] is False
    assert (player._mirror.read_idx, player._native_gain, player._callback_calls, player._underflow_count, player._starvation_gaps,
            player._tail_ramp_samples, player._played_samples) == (77, 0.5, 3, 2, 1, 128, 99)
    assert player._mirror.acked_seq == 2**40 + 3
    print("swift frames decoded by the host: hello, say (spoken, typed), ping, barge, cancel, READY, REPORT x2, STATUS, DISCARD_ACK")
