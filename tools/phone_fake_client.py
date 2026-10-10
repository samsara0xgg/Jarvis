"""A fake phone for ADR 0209: talks to a running daemon over ``/phone/ws`` and plays like the app.

    .venv/bin/python tools/phone_fake_client.py --url ws://100.x.y.z:8006/phone/ws \
        --token-file ~/.jarvis-phone-token --say "what time is it" [--typed] \
        [--barge-after 2.0] [--wav /tmp/answer.wav] [--timeout 30]

It sends ``hello``, the binary READY, a ping and one ``say``, then renders the PCM16 frames the
host sends at real-time pace, answers with REPORT and STATUS and acknowledges every DISCARD, as
the phone's renderer must (:mod:`jarvis.surface.phone_player` has the wire). It prints what
arrived (rows, audio seconds, first-audio delay) and writes everything it played to a WAV for
listening. The device token is read from ``--token-file`` (a file only you can read) and is never
printed or taken from the command line. Run it against a daemon with ``MINIMAX_API_KEY`` set to
hear her; without one the host answers ``voice: false`` and this prints only the rows.
"""

# ruff: noqa: T201, S101, D107, D102, D103, E501, C901, PLR0915

from __future__ import annotations

import argparse
import contextlib
import json
import struct
import sys
import threading
import time
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

import numpy as np
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

if TYPE_CHECKING:
    from collections.abc import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis.surface.phone_player import PCM16, PHONE_SAMPLE_RATE_HZ

RATE = PHONE_SAMPLE_RATE_HZ
DEFAULT_CLOCK_OFFSET_NS = 7_000_000_000  # the phone's clock is not the host's
PRESENTATION_DELAY_NS = 20_000_000
# Host -> phone and phone -> host frame types of jarvis/surface/phone_player.py.
ACTIVE, DISCARD, GAIN, HOLD = 2, 3, 4, 5
READY, REPORT, STATUS, DISCARD_ACK = 0x81, 0x82, 0x83, 0x84


class FakePhone:
    """A paired phone: ``hello``, READY, a renderer at real-time pace, and a log of what came."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        voice: bool = True,
        speed: float = 1.0,
        clock_offset_ns: int = DEFAULT_CLOCK_OFFSET_NS,
    ) -> None:
        # The proxy variables of a sandbox must not carry a socket to a private address.
        self.ws = connect(
            url, additional_headers={"Authorization": f"Bearer {token}"}, proxy=None,
        )
        self.voice = voice
        self.speed = speed
        self.clock_offset_ns = clock_offset_ns
        self.heard: list[np.ndarray] = []  # everything the renderer played, in order
        self.last_played_at = time.monotonic()
        self.lock = threading.Condition()
        self.texts: list[dict[str, Any]] = []
        self.binary_types: list[int] = []
        self.pcm_samples: dict[int, int] = {}  # generation -> samples received
        self.chunks: list[tuple[int, int, np.ndarray]] = []  # (generation, cursor, samples)
        self.order: list[tuple[str, int]] = []  # ("pcm"|"discard"|"report"|"ack", generation|seq)
        self.active = -1
        self.held_generation = -1
        self.held_locally = False
        self.written = 0
        self.read_idx = 0
        self.discard_before = 0
        self.discard_seq = 0
        self.acked_seq = 0
        self.played = 0
        self.played_per_generation: dict[int, int] = {}
        self.played_at_discard: dict[int, int] = {}
        self.first_reported: set[int] = set()
        self.closed: ConnectionClosed | None = None
        self._stop = threading.Event()
        self._last_tick = time.monotonic()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._renderer = threading.Thread(target=self._render, daemon=True)

    def phone_ns(self) -> int:
        """The phone's own monotonic clock: deliberately not the host's."""
        return time.monotonic_ns() + self.clock_offset_ns

    def start(self) -> Self:
        self._reader.start()
        self._renderer.start()
        return self

    def close(self) -> None:
        self._stop.set()
        with contextlib.suppress(Exception):
            self.ws.close()

    # -- control

    def send(self, frame: dict[str, Any]) -> None:
        self.ws.send(json.dumps(frame))

    def hello(self) -> dict[str, Any]:
        self.send({"type": "hello", "voice": self.voice})
        return self.wait_text("ready")

    def send_ready(self) -> None:
        body = struct.pack("<IIqq", RATE, 512, PRESENTATION_DELAY_NS, self.phone_ns())
        self.ws.send(bytes([READY]) + body)

    def say(self, utterance_id: str, text: str, *, spoken: bool) -> None:
        self.send({
            "type": "say", "utterance_id": utterance_id, "text": text, "spoken": spoken,
            "language": "en", "confidence": 0.9,
        })

    def wait_text(self, kind: str, timeout_s: float = 10.0, *, after: int = 0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        with self.lock:
            while True:
                for frame in self.texts[after:]:
                    if frame.get("type") == kind:
                        return frame
                remaining = deadline - time.monotonic()
                assert remaining > 0, f"no {kind} frame arrived: {self._types()}"
                self.lock.wait(min(remaining, 0.1))

    def _types(self) -> list[str]:
        return [str(frame.get("type")) for frame in self.texts]

    def rows(self) -> list[dict[str, Any]]:
        with self.lock:
            return [f for f in self.texts if f.get("type") == "row"]

    def wait_until(self, condition: Callable[[], bool], what: str, timeout_s: float = 15.0) -> None:
        deadline = time.monotonic() + timeout_s
        while not condition():
            assert time.monotonic() < deadline, what
            time.sleep(0.01)

    # -- the reader: frames from the host

    def _read(self) -> None:
        try:
            for message in self.ws:
                if isinstance(message, bytes):
                    self._on_binary(message)
                else:
                    with self.lock:
                        self.texts.append(json.loads(message))
                        self.lock.notify_all()
        except ConnectionClosed as exc:
            with self.lock:
                self.closed = exc
                self.lock.notify_all()

    def _on_binary(self, data: bytes) -> None:
        kind, body = data[0], memoryview(data)[1:]
        with self.lock:
            self.binary_types.append(kind)
            if kind == PCM16:
                generation, cursor = struct.unpack_from("<qq", body)
                samples = np.frombuffer(body[16:], dtype="<i2").copy()
                self.chunks.append((generation, cursor, samples))
                self.written += len(samples)
                self.pcm_samples[generation] = self.pcm_samples.get(generation, 0) + len(samples)
                self.order.append(("pcm", generation))
            elif kind == ACTIVE:
                (self.active,) = struct.unpack("<q", body)
            elif kind == DISCARD:
                (self.discard_seq,) = struct.unpack("<Q", body)
                self.discard_before = self.written
                self.order.append(("discard", self.discard_seq))
            elif kind == HOLD:
                (self.held_generation,) = struct.unpack("<q", body)
            elif kind == GAIN:
                pass
            else:
                msg = f"the host sent a frame type the protocol does not define: {kind}"
                raise AssertionError(msg)

    # -- the renderer: real-time playback of what was sent

    def hold_output(self) -> None:
        """The phone heard Allen over her voice and holds its output at once."""
        with self.lock:
            self.held_locally = True

    def _render(self) -> None:
        with contextlib.suppress(ConnectionClosed):
            self._render_until_closed()

    def _render_until_closed(self) -> None:
        while not self._stop.wait(0.005):
            now = time.monotonic()
            due = int((now - self._last_tick) * RATE * self.speed)
            self._last_tick += due / (RATE * self.speed)
            reports: list[tuple[int, int, int]] = []
            ack: int | None = None
            with self.lock:
                if self.discard_seq > self.acked_seq:
                    self._apply_discard()
                    ack = self.discard_seq
                reports = [] if self.held_locally else self._play(due)
                status = struct.pack(
                    "<qdQQQIQ", self.read_idx, 1.0, 0, 0, 0, 0, self.played,
                )
            for generation, start, end in reports:
                first = generation not in self.first_reported
                self.first_reported.add(generation)
                body = struct.pack(
                    "<qqqBqqB", generation, start, end, 0, self.phone_ns(), PRESENTATION_DELAY_NS,
                    int(first),
                )
                with self.lock:
                    self.order.append(("report", generation))
                self.ws.send(bytes([REPORT]) + body)
            self.ws.send(bytes([STATUS]) + status)
            if ack is not None:
                with self.lock:
                    self.order.append(("ack", ack))
                    self.acked_seq = ack
                self.ws.send(bytes([DISCARD_ACK]) + struct.pack("<Q", ack))

    def _apply_discard(self) -> None:
        """Drop every sample sent before the DISCARD frame, as the helper's render thread does."""
        for generation in {g for g, _, _ in self.chunks} | set(self.played_per_generation):
            self.played_at_discard[generation] = self.played_per_generation.get(generation, 0)
        while self.chunks and self.read_idx < self.discard_before:
            _generation, _cursor, samples = self.chunks[0]
            take = min(len(samples), self.discard_before - self.read_idx)
            self.read_idx += take
            if take == len(samples):
                self.chunks.pop(0)
            else:
                self.chunks[0] = (_generation, _cursor + take, samples[take:])
        self.held_locally = False

    def _play(self, due: int) -> list[tuple[int, int, int]]:
        reports: list[tuple[int, int, int]] = []
        while due > 0 and self.chunks:
            generation, cursor, samples = self.chunks[0]
            if generation != self.active:
                self.read_idx += len(samples)  # not the active generation: skipped, not played
                self.chunks.pop(0)
                continue
            if self.held_generation == generation:
                break
            take = min(due, len(samples))
            reports.append((generation, cursor, cursor + take))
            self.read_idx += take
            self.played += take
            self.heard.append(samples[:take])
            self.last_played_at = time.monotonic()
            self.played_per_generation[generation] = (
                self.played_per_generation.get(generation, 0) + take
            )
            due -= take
            if take == len(samples):
                self.chunks.pop(0)
            else:
                self.chunks[0] = (generation, cursor + take, samples[take:])
        return reports


def _read_token(path: Path) -> str:
    if path.stat().st_mode & 0o077:
        sys.exit(f"{path} is readable by others; chmod 600 it")
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        sys.exit(f"{path} is empty")
    return token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--url", required=True, help="ws://host:port/phone/ws")
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--say", default="Hello, who is this?")
    parser.add_argument("--typed", action="store_true", help="send it as typed text (no audio)")
    parser.add_argument("--no-voice", action="store_true", help="hello with voice: false")
    parser.add_argument("--barge-after", type=float, help="seconds of audio, then barge in")
    parser.add_argument("--wav", type=Path, help="write what was played here (32 kHz mono)")
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds to wait for it all")
    args = parser.parse_args(argv)

    phone = FakePhone(args.url, _read_token(args.token_file), voice=not args.no_voice).start()
    try:
        ready = phone.hello()
        print(
            f"ready: device={ready.get('device')} voice={ready.get('voice')} "
            f"sample_rate={ready.get('sample_rate')}",
        )
        if ready.get("voice"):
            phone.send_ready()
        deadline = time.monotonic() + args.timeout
        phone.send({"type": "ping", "t_ns": phone.phone_ns()})
        pong = phone.wait_text("pong")
        print(f"pong: host clock minus phone clock = {pong['host_ns'] - pong['t_ns']} ns")
        phone.say(f"fake-{int(time.time())}", args.say, spoken=not args.typed)
        said = phone.wait_text("said")
        print(f"said: turn_id={said['turn_id']}")
        t_said = time.monotonic()

        first_audio = barged_at = None
        while time.monotonic() < deadline:
            time.sleep(0.05)
            with phone.lock:
                played, closed = phone.played, phone.closed
                ended = {
                    r["event_type"] for r in phone.texts if r.get("type") == "row"
                } & {"surface.response_emitted", "response.cancelled", "response.failed"}
                idle_s = time.monotonic() - phone.last_played_at
            if first_audio is None and played > 0:
                first_audio = time.monotonic() - t_said
            if args.barge_after is not None and barged_at is None and played >= args.barge_after * RATE:
                phone.hold_output()
                phone.send({"type": "barge"})
                barged_at = played / RATE
            if closed is not None:
                print(f"closed by the host: {closed.rcvd.code if closed.rcvd else 'no code'}")
                break
            silent = not ready.get("voice") or args.typed
            if (ended and silent) or (ended and played > 0 and idle_s > 1.0):
                break
            if barged_at is not None and phone.acked_seq > 0 and idle_s > 1.0:
                break
        rows = phone.rows()
        text = "".join(
            r["payload"].get("text", "") for r in rows if r["event_type"] == "surface.response_chunk"
        )
        print(f"rows: {[r['event_type'] for r in rows]}")
        print(f"answer text: {text!r}")
        with phone.lock:
            heard = np.concatenate(phone.heard) if phone.heard else np.zeros(0, dtype="<i2")
            print(
                f"audio: {phone.written / RATE:.2f} s received, {phone.played / RATE:.2f} s "
                f"played, frame types seen {sorted(set(phone.binary_types))}",
            )
            acked = phone.acked_seq > 0
        if first_audio is not None:
            print(f"first audio {first_audio * 1000:.0f} ms after the say was acknowledged")
        if barged_at is not None:
            print(f"barged in at {barged_at:.2f} s of audio; host asked to discard, acked: {acked}")
        if args.wav is not None and heard.size:
            with wave.open(str(args.wav), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(RATE)
                out.writeframes(heard.astype("<i2").tobytes())
            print(f"wrote {args.wav}")
        errors = [t for t in phone.texts if t.get("type") == "error"]
        if errors:
            print(f"errors: {errors}")
            return 1
        return 0
    finally:
        phone.close()


if __name__ == "__main__":
    sys.exit(main())
