"""The phone route's hosts and daemons: a real daemon over a log, voice and step hosts."""

from __future__ import annotations

import asyncio
import base64
import functools
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Self, cast

import httpx
import numpy as np
import uvicorn
from fastapi.testclient import TestClient

from jarvis.decision import DecideContext, decide
from jarvis.decision.tier0 import load_tier0_table
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.runtime import inherent_loop
from jarvis.runtime.phone_voice import PhoneSpeech
from jarvis.state.attachments import ATTACHMENTS_DIRNAME, AttachmentRef, Attachments
from jarvis.state.device_tokens import device_name_for_token, device_token_matches, pair_device
from jarvis.state.event_log import (
    emit_event,
    iter_events_for_turn,
    iter_events_of_types,
    open_event_log,
    open_runtime_event_log,
)
from jarvis.state.memory_db import append_record, conversation_rows, render_context
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.state.turn_steps import answer_did
from jarvis.surface import voice_media, voice_tts
from jarvis.surface.cli import emit_surface_user_intent
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import AskOutcome, InherentDeps, create_app, require_local_key
from jarvis.surface.phone_link import PHONE_PATH, PhoneHub
from jarvis.surface.phone_player import PHONE_SAMPLE_RATE_HZ, PhoneAudioStreamPlayer
from jarvis.surface.terminal_events import BrainEvents
from jarvis.surface.voice_tts import TTSAudioChunk, TTSResponseSegment, TTSSegmentFinished
from tests.support.media import CallbackPump
from tests.support.media import media_config as _mac_config
from tests.support.model import ScriptedModel
from tests.support.net import free_port
from tests.support.runtime_paths import StubRuntimePaths
from tests.support.terminal import AsRemote, VoiceSpeechProvider, wait_for
from tools.phone_fake_client import FakePhone

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from httpx2 import Response

    from jarvis.decision import LifecycleLike, RuntimePathsLike, ToolRegistryLike
    from jarvis.state.voiceprint import VoiceprintBook


def own_private_address() -> str | None:
    """The machine's own private non-loopback IPv4 address, else ``None``.

    Connecting a UDP socket sends nothing; it only makes the kernel pick the outgoing address.
    A client that connects to that address from this machine is seen by the server as coming
    from it, a peer that is not on loopback.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))
        except OSError:
            return None
        address: str = probe.getsockname()[0]
    parts = [int(part) for part in address.split(".")]
    private = (
        parts[0] == 10
        or parts[:2] == [192, 168]
        or (parts[0] == 172 and 16 <= parts[1] < 32)
        or (parts[0] == 100 and 64 <= parts[1] < 128)
        or parts[:3] == [192, 0, 2]
    )
    return address if private else None


def bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


@contextmanager
def alone_daemon(tmp_path: Path, address: str) -> Iterator[tuple[Path, int]]:
    """A real role-all daemon listening on loopback and on ``address``: its root and port."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "settings.yaml").write_text(
        "runtime:\n  role: all\n"
        f"  listen_addresses: ['{address}']\n  listen_hosts: ['{address}']\n",
        encoding="utf-8",
    )
    # Voice models on disk (empty stand-ins), so the daemon does not fetch real ones; building
    # the voice pipeline from them fails and the daemon keeps serving text.
    sensevoice = root / "models" / "sensevoice-small-int8"
    sensevoice.mkdir(parents=True)
    (sensevoice / "model.int8.onnx").write_bytes(b"x")
    (sensevoice / "tokens.txt").write_text("", encoding="utf-8")
    (root / "models" / "silero_vad.onnx").write_bytes(b"x")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.endswith(("_API_KEY", "_ADMIN_KEY")) and name != "JARVIS_RUNTIME_ROOT"
    }
    env.update(HOME=str(tmp_path), JARVIS_LOG_LEVEL="INFO")
    log = tmp_path / "daemon.log"
    with log.open("w") as sink:
        daemon = subprocess.Popen(  # noqa: S603 — sys.executable and a fixed argv.
            [
                sys.executable,
                "-m",
                "jarvis",
                "serve",
                "--runtime-root",
                str(root),
                "--port",
                str(port),
            ],
            stdout=sink,
            stderr=subprocess.STDOUT,
            env=env,
        )
    try:
        deadline = time.monotonic() + 40
        while True:
            try:
                if httpx.get(f"http://{address}:{port}/api/health", timeout=2).json() == {
                    "status": "ok",
                }:
                    break
            except httpx.HTTPError:
                pass
            assert daemon.poll() is None, log.read_text(encoding="utf-8")[-2000:]
            assert time.monotonic() < deadline, "daemon never answered on its private address"
            time.sleep(0.2)
        yield root, port
    finally:
        daemon.terminate()
        daemon.wait(timeout=20)


REMOTE = "100.87.250.92"


LOOPBACK = "127.0.0.1"


def here_daemon(tmp_path: Path) -> tuple[Path, str, str, TestClient, TestClient]:
    """A daemon's routes over a real log: ``(log, local key, device token, loopback, tailnet)``."""
    root = tmp_path / "root"
    root.mkdir()
    log = root / "events.db"
    open_event_log(log).close()

    def submit_plain(text: str) -> str:
        with closing(open_runtime_event_log(log)) as conn:
            turn = f"T{int(time.time_ns())}"
            emit_surface_user_intent(conn, transcript=text, turn_id=turn)
            return turn

    deps = InherentDeps(
        submit_callable=submit_plain,
        broadcaster=InherentBroadcaster(),
        attachments=Attachments(root / "artifacts" / ATTACHMENTS_DIRNAME),
        images_ok=True,
        submit_attachments=functools.partial(inherent_loop._submit_with_attachments, log),  # noqa: SLF001
        submit_as_device=functools.partial(inherent_loop._submit_as_device, log),  # noqa: SLF001
        share_callable=functools.partial(inherent_loop._keep_share, log),  # noqa: SLF001
        ask_outcome=lambda _turn: AskOutcome(spoken="ok"),
        device_name=functools.partial(device_name_for_token, root),
    )
    app = create_app(deps)
    key = local_key(root)
    require_local_key(
        app,
        functools.partial(local_key_matches, key),
        extra_hosts=[REMOTE],
        device_token_matches=functools.partial(device_token_matches, root),
    )
    token = pair_device(root, "iphone")
    return (
        log,
        key,
        token,
        TestClient(app, base_url=f"http://{LOOPBACK}:8006", client=(LOOPBACK, 50000)),
        TestClient(app, base_url=f"http://{REMOTE}:8006", client=(REMOTE, 50000)),
    )


TIER0_TABLE = Path(__file__).parents[2] / "config" / "tier0_patterns.yaml"


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
)


NOTES = b"Dinner at 7\nBring the red folder\n"


@dataclass
class Daemon:
    """The app behind the local key and device tokens, with real callables on a real log."""

    root: Path
    log: Path
    store: Attachments
    key: str
    token: str
    loopback: TestClient
    remote: TestClient

    def events(self, event_type: str) -> list[dict[str, Any]]:
        """The payloads of the log's events of one type, oldest first."""
        with closing(open_runtime_event_log(self.log)) as conn:
            return [dict(e.payload) for e in iter_events_of_types(conn, [event_type])]

    def intents(self) -> list[dict[str, Any]]:
        """The turns started so far."""
        return self.events("surface.user_intent")

    def upload(self, content: bytes, name: str = "photo.png", *, via: str = "remote") -> Response:
        """Post one file with the credential that fits the peer."""
        client = self.remote if via == "remote" else self.loopback
        headers = {"Authorization": f"Bearer {self.token if via == 'remote' else self.key}"}
        return client.post(
            "/inherent/attachments",
            files={"file": (name, content, "application/octet-stream")},
            headers=headers,
        )

    def auth(self) -> dict[str, str]:
        """The paired phone's header."""
        return {"Authorization": f"Bearer {self.token}"}


def attachments_daemon(
    tmp_path: Path,
    *,
    images_ok: bool = True,
    wired: bool = True,
    saved: Callable[[AttachmentRef], None] | None = None,
) -> Daemon:
    root = tmp_path / "root"
    root.mkdir()
    log = root / "events.db"
    open_event_log(log).close()
    store = Attachments(root / "artifacts" / ATTACHMENTS_DIRNAME)

    def submit_plain(text: str) -> str:
        with closing(open_runtime_event_log(log)) as conn:
            event = emit_surface_user_intent(conn, transcript=text, turn_id="T-plain")
            return str(event.payload["turn_id"])

    deps = InherentDeps(
        submit_callable=submit_plain,
        broadcaster=InherentBroadcaster(),
        attachments=store if wired else None,
        attachment_saved=saved,
        images_ok=images_ok,
        submit_attachments=functools.partial(inherent_loop._submit_with_attachments, log)  # noqa: SLF001
        if wired
        else None,
        share_callable=functools.partial(inherent_loop._keep_share, log)  # noqa: SLF001
        if wired
        else None,
    )
    app = create_app(deps)
    key = local_key(root)
    require_local_key(
        app,
        functools.partial(local_key_matches, key),
        extra_hosts=[REMOTE],
        device_token_matches=functools.partial(device_token_matches, root),
    )
    token = pair_device(root, "iphone")
    return Daemon(
        root=root,
        log=log,
        store=store,
        key=key,
        token=token,
        loopback=TestClient(app, base_url=f"http://{LOOPBACK}:8006", client=(LOOPBACK, 50000)),
        remote=TestClient(app, base_url=f"http://{REMOTE}:8006", client=(REMOTE, 50000)),
    )


ANSWER = "It is a one pixel picture."
"""What the model says to a picture turn."""


def attachment_turn(  # noqa: PLR0913 — one keyword per thing a turn can be given.
    d: Daemon,
    said: str,
    ids: Sequence[str],
    *,
    live_context: tuple[str, ...] = (),
    tier0: bool = False,
    sent: list[str] | None = None,
    history: Sequence[dict[str, Any]] = (),
) -> tuple[ScriptedModel, str]:
    """Submit over HTTP, then run the stored intent through the real ``decide()``."""
    posted = d.remote.post(
        "/inherent/submit",
        headers=d.auth(),
        json={"text": said, "attachments": list(ids)},
    )
    assert posted.status_code == 200
    model = ScriptedModel(otherwise=ANSWER)
    paths = StubRuntimePaths(event_log=d.log, artifacts_root=d.root / "artifacts")
    with closing(open_event_log(d.log)) as conn:
        turn_id = posted.json()["turn_id"]
        (trigger,) = list(iter_events_for_turn(conn, turn_id, ["surface.user_intent"]))
        ctx = DecideContext(
            conn=conn,
            runtime_paths=cast("RuntimePathsLike", paths),
            tool_registry=cast("ToolRegistryLike", build_default_registry()),
            lifecycle=cast("LifecycleLike", ActionLifecycle()),
            llm_client=model.as_llm_client(),
            system_prompt="stub system prompt",
            history=history,
            time_note="Time: 2026-10-10T12:00-07:00 Saturday",
            live_context=live_context,
            read_attachments=d.store.load_many,
            record_sent_message=None if sent is None else sent.append,
            tier0_table=load_tier0_table(TIER0_TABLE) if tier0 else None,
        )
        result = decide(trigger, ctx)
    assert result.response_plan is not None
    return model, result.response_plan.text


TURN_WORDS = "把它推到明天"


RECENT = "今天天气很好适合出门散步"


@dataclass
class JudgeRig:
    """The route over a real log with fake host actions; ``calls`` is their order."""

    root: Path
    calls: list[tuple[Any, ...]] = field(default_factory=list)
    asked: list[tuple[Any, ...]] = field(default_factory=list)
    elsewhere_asked: list[tuple[str, str, str]] = field(default_factory=list)
    log: Path = field(init=False)
    token: str = field(init=False)
    client: TestClient = field(init=False)

    def build(
        self,
        *,
        quiet: bool = True,
        ask: Callable[..., str | None] | None = None,
        recent: str | None = RECENT,
        elsewhere: Callable[[str, str, str], bool] | None = None,
        voiceprints: VoiceprintBook | None = None,
    ) -> None:
        self.root.mkdir(exist_ok=True)
        self.log = self.root / "events.db"
        open_event_log(self.log).close()
        conn = sqlite3.connect(self.log, check_same_thread=False)
        self.token = pair_device(self.root, "iphone")

        async def open_voice(_device: str, _send: Callable[[bytes], bool]) -> Any:  # noqa: ANN401
            async def stop_playback(reason: str) -> str:
                self.calls.append(("stop_playback", reason))
                return "stopped"

            async def nothing() -> None:
                return None

            return SimpleNamespace(
                player=SimpleNamespace(
                    connection_lost=lambda: None,
                    is_running=False,
                    observe_clock=lambda *_a: None,
                ),
                begin=nothing,
                close=nothing,
                stop_playback=stop_playback,
            )

        def barge_in(device: str) -> str:
            self.calls.append(("barge_in", device))
            return "cancelled"

        def cancel_turn(turn_id: str, reason: str) -> str:
            self.calls.append(("cancel_turn", turn_id, reason))
            return "cancelled"

        def set_quiet(level: str) -> None:
            self.calls.append(("set_quiet", level))

        def ask_words(*args: Any) -> str | None:  # noqa: ANN401
            self.asked.append(args)
            return None if ask is None else ask(*args)

        def heard_elsewhere(device: str, turn_id: str, text: str) -> bool:
            self.elsewhere_asked.append((device, turn_id, text))
            return False if elsewhere is None else elsewhere(device, turn_id, text)

        hub = PhoneHub(
            events=BrainEvents(conn),
            rows=inherent_loop._PhoneRows(conn),  # noqa: SLF001
            open_voice=open_voice,
            barge_in=barge_in,
            cancel_turn=cancel_turn,
            ask_words=ask_words,
            note_words=lambda *args: self.calls.append(("note_words", args[1])),
            begin_line=lambda *args: self.calls.append(("begin_line", args[0])),
            recent_speech=None if recent is None else (lambda: recent),
            set_quiet=set_quiet if quiet else None,
            heard_elsewhere=None if elsewhere is None else heard_elsewhere,
            voiceprints=voiceprints,
        )
        app = create_app(
            InherentDeps(
                submit_callable=lambda _text: "T1",
                broadcaster=InherentBroadcaster(),
                phone=hub,
                device_name=lambda one: device_name_for_token(self.root, one),
            ),
        )
        require_local_key(
            app,
            lambda header: local_key_matches(local_key(self.root), header),
            extra_hosts=[REMOTE],
            device_token_matches=lambda one: device_token_matches(self.root, one),
        )
        self.client = TestClient(app, base_url=f"http://{REMOTE}:8006", client=(REMOTE, 50000))

    def connect(self) -> Any:  # noqa: ANN401 - a Starlette test websocket context
        return self.client.websocket_connect(
            f"ws://{REMOTE}:8006{PHONE_PATH}",
            headers={"Authorization": f"Bearer {self.token}"},
        )

    def turns(self) -> list[str]:
        with sqlite3.connect(self.log) as raw:
            return [
                row[0]
                for row in raw.execute(
                    "SELECT json_extract(payload_json, '$.turn_id') FROM events "
                    "WHERE type IN ('utterance.received', 'surface.user_intent') ORDER BY id",
                )
            ]


def open_ws(ws: Any) -> dict[str, Any]:  # noqa: ANN401
    ws.send_json({"type": "hello", "voice": True})
    ready = ws.receive_json()
    assert ready["type"] == "ready"
    return dict(ready)


def say_words(
    ws: Any,  # noqa: ANN401
    utterance_id: str,
    text: str = TURN_WORDS,
    *,
    spoken: bool = True,
    **flags: Any,  # noqa: ANN401
) -> dict[str, Any]:
    ws.send_json(
        {"type": "say", "utterance_id": utterance_id, "text": text, "spoken": spoken, **flags},
    )
    while (reply := ws.receive_json())["type"] not in {"said", "error"}:
        pass
    return dict(reply)


NOW = int(time.time() * 1000)


MINUTE = 60_000


HOUR = 60 * MINUTE


UUID = "3F2A9C1E-7B3D-4C55-8E21-0A1B2C3D4E5F"


GOOD: dict[str, dict[str, Any]] = {
    "phone.visit_observed": {
        "lat": 48.46,
        "lng": -123.31,
        "accuracy_m": 65.0,
        "arrived_at_ms": NOW - 3 * HOUR,
        "departed_at_ms": NOW - HOUR,
        "place": "University of Victoria",
    },
    "phone.location_observed": {
        "lat": 48.4284,
        "lng": -123.3656,
        "accuracy_m": 120,
        "place": "Douglas St, Victoria",
        "speed_mps": 1.4,
    },
    "phone.motion_observed": {
        "activity": "walking",
        "confidence": "high",
        "started_at_ms": NOW - 15 * MINUTE,
        "ended_at_ms": NOW - MINUTE,
    },
    "phone.health_observed": {
        "metric": "workout",
        "start_ms": NOW - 2 * HOUR,
        "end_ms": NOW - HOUR,
        "value": 3600,
        "detail": "running",
        "unit": "s",
        "source": "Allen's Apple Watch",
        "device": "Watch7,1",
        "sample_uuid": UUID,
        "meta": {"indoor": False, "avg_heart_rate": 151.5, "time_zone": "America/Vancouver"},
    },
    "phone.state_observed": {"signal": "focus", "value": "on", "detail": "Work"},
    "phone.measure_observed": {
        "metric": "heart_rate",
        "start_ms": NOW - 3 * MINUTE,
        "end_ms": NOW - 3 * MINUTE,
        "value": 64.0,
        "unit": "count/min",
        "sample_uuid": UUID,
        "source": "Allen's Apple Watch",
        "device": "Watch7,1",
        "meta": {"motion_context": "sedentary"},
    },
    # ADR 0312: three fixes of a drive, the second with nothing iOS could not measure
    "phone.track_observed": {
        "started_at_ms": NOW - 2 * MINUTE,
        "ended_at_ms": NOW - 2 * MINUTE + 2000,
        "fixes": [
            [0, 48.4284123, -123.3656456, 4.7, 21.3, 3.1, 13.42, 0.6, 271.5, 4.2, 0],
            [1000, 48.4284201, -123.3658311, 4.7, None, None, None, None, None, None, 0],
            [2000, 48.4284299, -123.3660122, 9.5, 21.1, 3.3, 13.9, 0.7, 0.0, 12.0, 3],
        ],
    },
}


def fixed_uid(n: int) -> str:
    return f"{n:032x}"


def event_frame(
    n: int,
    kind: str,
    payload: dict[str, Any] | None = None,
    **extra: Any,  # noqa: ANN401
) -> dict[str, Any]:
    return {
        "event_uid": fixed_uid(n),
        "event_type": kind,
        "ts_epoch_ms": NOW,
        "payload": GOOD[kind] if payload is None else payload,
        **extra,
    }


VISIT, LOCATION, MOTION, HEALTH, STATE, MEASURE, TRACK = (
    "phone.visit_observed",
    "phone.location_observed",
    "phone.motion_observed",
    "phone.health_observed",
    "phone.state_observed",
    "phone.measure_observed",
    "phone.track_observed",
)


RATE = PHONE_SAMPLE_RATE_HZ


CHAR_SAMPLES = 3_200  # the fake provider speaks 100 ms a character at 32 kHz


def _speak32(segment: TTSResponseSegment) -> list[Any]:
    samples = CHAR_SAMPLES * len(segment.text)
    wave = (np.arange(samples) % 200 * 40 - 4000).astype("<i2").tobytes()
    boundaries = tuple((i + 1, 100.0 * (i + 1)) for i in range(len(segment.text)))
    return [
        TTSAudioChunk(segment.sequence, wave, RATE),
        TTSAudioChunk(
            segment.sequence,
            b"",
            RATE,
            timing_text=segment.text,
            word_boundaries=boundaries,
        ),
        TTSSegmentFinished(segment.sequence, usage={"usage_characters": len(segment.text)}),
    ]


def _config32() -> voice_media.StreamingMediaConfig:
    return replace(
        _mac_config(),
        canonical_sample_rate_hz=RATE,
        ring_seconds=0.5,
        response_timeout_s=30.0,
    )


def voice_log(path: Path) -> sqlite3.Connection:
    open_event_log(path).close()
    return sqlite3.connect(path, check_same_thread=False)


class VoiceHost:
    """A uvicorn server in this process with the phone route, as the daemon wires it."""

    def __init__(self, root: Path, *, voice: bool = True, remote: bool = True) -> None:
        self.root = root
        self.log = root / "events.db"
        voice_log(self.log).close()
        self.token = pair_device(root, "iphone")
        self.second_token = pair_device(root, "ipad")
        self.port = free_port()
        self.voice = voice
        self.remote = remote
        self.phone_provider = VoiceSpeechProvider(script=_speak32)
        self.mac_provider = VoiceSpeechProvider()
        self.barges: list[str] = []
        self._barged = threading.Condition()
        self.cancels: list[tuple[str, str]] = []
        self.voices: list[Any] = []
        self.hub: PhoneHub | None = None
        self.server: uvicorn.Server | None = None
        self.thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)

    def _build_phone_pipeline(
        self,
        player: PhoneAudioStreamPlayer,
        speaks_turn: Callable[[sqlite3.Connection, str], bool],
        boot: int,
    ) -> voice_media.StreamingTTSPipeline:
        return voice_media.StreamingTTSPipeline(
            provider=cast("Any", self.phone_provider),
            player=player,
            conn_factory=lambda: open_event_log(self.log),
            boot_high_water_id=boot,
            config=_config32(),
            start_player=False,
            speaks_turn=speaks_turn,
        )

    def _barge(self, device: str) -> str:
        with self._barged:
            self.barges.append(device)
            self._barged.notify_all()
        return "cancelled"

    def barged(self, count: int = 1, timeout_s: float = 15.0) -> list[str]:
        """The devices whose answer was cancelled by a barge, once ``count`` have been.

        The hook runs on a worker after the actor has settled the interruption, so a check made
        when the interrupted row appears waits for the arrival instead of racing it.
        """
        with self._barged:
            self._barged.wait_for(lambda: len(self.barges) >= count, timeout_s)
            return list(self.barges)

    def _cancel(self, turn: str, reason: str) -> str:
        self.cancels.append((turn, reason))
        return "cancelled"

    async def _main(self) -> None:
        conn = sqlite3.connect(self.log, check_same_thread=False)
        events = BrainEvents(conn)
        rows = inherent_loop._PhoneRows(conn)  # noqa: SLF001
        speech = PhoneSpeech(
            build=self._build_phone_pipeline,
            rows=rows,
            ring_seconds=0.5,
            event_log=self.log,
        )

        async def open_voice(device: str, send_binary: Callable[[bytes], bool]) -> Any:  # noqa: ANN401
            voice = await speech.open(device, send_binary)
            self.voices.append(voice)
            return voice

        hub = PhoneHub(
            events=events,
            rows=rows,
            open_voice=open_voice if self.voice else None,
            barge_in=self._barge,
            cancel_turn=self._cancel,
        )
        self.hub = hub
        mac_player = voice_tts.AudioStreamPlayer(
            sample_rate_hz=8_000,
            ring_seconds=0.5,
            lazy_open=True,
            generation_safe=True,
            estimated_output_latency_s=0.0,
        )
        mac = voice_media.StreamingTTSPipeline(
            provider=cast("Any", self.mac_provider),
            player=mac_player,
            conn_factory=lambda: open_event_log(self.log),
            boot_high_water_id=inherent_loop._latest_id(conn),  # noqa: SLF001
            config=_mac_config(),
            start_player=False,
            silent_intent_channels=inherent_loop._TTS_SILENT_CHANNELS,  # noqa: SLF001
        )
        watcher = asyncio.create_task(
            inherent_loop._tts_watcher(conn=conn, pipeline=mac, poll_interval_s=0.01),  # noqa: SLF001
        )
        ends = asyncio.create_task(  # ADR 0222: the daemon runs this beside the other watchers
            inherent_loop._phone_ends_watcher(  # noqa: SLF001
                conn,
                speech.owner_boot,
                poll_interval_s=0.01,
            ),
        )
        app = create_app(
            InherentDeps(
                submit_callable=lambda _text: "T1",
                broadcaster=InherentBroadcaster(),
                phone=hub,
                device_name=functools.partial(device_name_for_token, self.root),
            ),
        )
        require_local_key(
            app,
            functools.partial(local_key_matches, local_key(self.root)),
            device_token_matches=functools.partial(device_token_matches, self.root),
        )
        self.server = uvicorn.Server(
            uvicorn.Config(
                AsRemote(app) if self.remote else app,
                host="127.0.0.1",
                port=self.port,
                log_level="warning",
                lifespan="off",
            ),
        )
        with CallbackPump(mac_player):
            try:
                await self.server.serve()
            finally:
                watcher.cancel()
                ends.cancel()
                await asyncio.gather(watcher, ends, return_exceptions=True)
                await asyncio.to_thread(mac.close)

    def __enter__(self) -> Self:
        self.thread.start()
        wait_for(lambda: bool(self.server and self.server.started), "server never started")
        return self

    def __exit__(self, *_exc: object) -> None:
        assert self.server is not None
        self.server.should_exit = True
        self.thread.join(timeout=20)

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}{PHONE_PATH}"

    def phone(
        self,
        *,
        token: str | None = None,
        voice: bool = True,
        speed: float = 1.0,
        report_overlap: int = 0,
        ack_delay_s: float = 0.0,
    ) -> FakePhone:
        return FakePhone(
            self.url,
            token or self.token,
            voice=voice,
            speed=speed,
            report_overlap=report_overlap,
            ack_delay_s=ack_delay_s,
        ).start()

    # -- the log

    def types(self, wanted: str) -> list[dict[str, Any]]:
        with sqlite3.connect(self.log) as raw:
            return [
                {**json.loads(row[0]), "_node": row[1], "_uid": row[2]}
                for row in raw.execute(
                    "SELECT payload_json, ingestion_node, event_uid FROM events WHERE type = ? "
                    "ORDER BY id",
                    (wanted,),
                )
            ]

    def playback(self, response_id: str) -> list[str]:
        with sqlite3.connect(self.log) as raw:
            return [
                row[0]
                for row in raw.execute(
                    "SELECT type FROM events WHERE type LIKE 'surface.playback_%' "
                    "AND json_extract(payload_json, '$.response_id') = ? ORDER BY id",
                    (response_id,),
                )
            ]


def answer_rows(
    log: Path,
    turn_id: str,
    response_id: str,
    text: str | list[str],
    emitted: dict[str, Any] | None = None,
) -> None:
    """The decision layer's answer rows for a turn: open, its chunks, emitted.

    One string is a finished text answer; a list is a stream, one speakable segment a chunk.
    ``emitted`` adds fields to the final ``surface.response_emitted`` row.
    """
    chunks = [text] if isinstance(text, str) else text
    conn = open_event_log(log)
    try:
        group = f"G-{response_id}"
        emit_event(
            conn,
            type="surface.response_open",
            payload={
                "turn_id": turn_id,
                "query": "q",
                "kind": "text" if isinstance(text, str) else "stream",
                "response_id": response_id,
                "response_group_id": group,
                "phase": "final",
                "channel": "speech",
            },
        )
        for sequence, chunk in enumerate(chunks):
            emit_event(
                conn,
                type="surface.response_chunk",
                payload={
                    "turn_id": turn_id,
                    "text": chunk,
                    "response_id": response_id,
                    "response_group_id": group,
                    "sequence": sequence,
                    "phase": "final",
                    "channel": "speech",
                    "segment_hash": "h",
                },
            )
        emit_event(
            conn,
            type="surface.response_emitted",
            payload={
                "turn_id": turn_id,
                "text": "".join(chunks),
                "response_id": response_id,
                "response_group_id": group,
                "phase": "final",
                "channel": "speech",
                **(emitted or {}),
            },
        )
    finally:
        conn.close()


class StepsHost:
    """The phone route, the attachment routes and the conversation route on one log."""

    def __init__(self, root: Path, *, files: bool = True) -> None:
        self.root = root
        self.files = files
        self.log = root / "events.db"
        self.memory = root / "memory.db"
        open_event_log(self.log).close()
        self.store = Attachments(root / "artifacts" / ATTACHMENTS_DIRNAME)
        self.token = pair_device(root, "iphone")
        self.other_token = pair_device(root, "ipad")
        self.port = free_port()
        self.server: uvicorn.Server | None = None
        self.thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)

    def _conversation(self, after: int, limit: int, before: int) -> dict[str, Any]:
        """What the daemon's ``_read_conversation`` does, with the real step lookup."""
        rows, more = conversation_rows(
            self.memory,
            since="",
            after=after,
            before=before,
            limit=limit,
        )
        inherent_loop._add_steps(self.log, rows)  # noqa: SLF001
        inherent_loop._add_places(self.log, rows)  # noqa: SLF001
        return {"since": "", "rows": rows, "has_more": more}

    async def _main(self) -> None:
        conn = sqlite3.connect(self.log, check_same_thread=False)
        hub = PhoneHub(
            events=BrainEvents(conn),
            rows=inherent_loop._PhoneRows(conn),  # noqa: SLF001
            check_attachments=self.store.check_for_turn if self.files else None,
        )
        submit_files = functools.partial(inherent_loop._submit_with_attachments, self.log)  # noqa: SLF001
        submit_as_device = functools.partial(inherent_loop._submit_as_device, self.log)  # noqa: SLF001
        app = create_app(
            InherentDeps(
                submit_callable=lambda _text: "T1",
                broadcaster=InherentBroadcaster(),
                phone=hub,
                device_name=functools.partial(device_name_for_token, self.root),
                attachments=self.store,
                submit_attachments=submit_files,
                submit_as_device=submit_as_device,
                conversation_read=self._conversation,
            ),
        )
        require_local_key(
            app,
            functools.partial(local_key_matches, local_key(self.root)),
            device_token_matches=functools.partial(device_token_matches, self.root),
        )
        self.server = uvicorn.Server(
            uvicorn.Config(
                AsRemote(app),
                host="127.0.0.1",
                port=self.port,
                log_level="warning",
                lifespan="off",
            ),
        )
        await self.server.serve()

    def __enter__(self) -> Self:
        self.thread.start()
        wait_for(lambda: bool(self.server and self.server.started), "server never started")
        return self

    def __exit__(self, *_exc: object) -> None:
        assert self.server is not None
        self.server.should_exit = True
        self.thread.join(timeout=20)

    def phone(self, token: str | None = None) -> FakePhone:
        phone = FakePhone(
            f"ws://127.0.0.1:{self.port}{PHONE_PATH}",
            token or self.token,
            voice=False,
        ).start()
        phone.hello()
        return phone

    def http(self, method: str, path: str, **kwargs: Any) -> httpx.Response:  # noqa: ANN401
        return httpx.request(
            method,
            f"http://127.0.0.1:{self.port}{path}",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=10,
            **kwargs,
        )

    def upload(self, content: bytes, name: str) -> str:
        sent = self.http("POST", "/inherent/attachments", files={"file": (name, content)})
        assert sent.status_code == 200, sent.text
        return str(sent.json()["id"])

    def intents(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self.log) as raw:
            return [
                {**json.loads(row[0]), "_node": row[1]}
                for row in raw.execute(
                    "SELECT payload_json, ingestion_node FROM events "
                    "WHERE type = 'surface.user_intent' ORDER BY id",
                )
            ]


def answer_with_record(
    host: StepsHost,
    turn_id: str,
    text: str,
    places: list[dict[str, Any]] | None = None,
) -> str:
    """Her answer's rows, and its memory record under the emitted row's uid; that uid."""
    conn = open_event_log(host.log)
    try:
        base = {"turn_id": turn_id, "response_id": f"R-{turn_id}", "phase": "final"}
        emit_event(
            conn,
            type="surface.response_open",
            payload={
                **base,
                "query": "q",
                "kind": "text",
                "response_group_id": "G",
                "channel": "speech",
            },
        )
        emit_event(
            conn,
            type="surface.response_chunk",
            payload={
                **base,
                "text": text,
                "response_group_id": "G",
                "sequence": 0,
                "channel": "speech",
                "segment_hash": "h",
            },
        )
        emitted = emit_event(
            conn,
            type="surface.response_emitted",
            payload={
                **base,
                "text": text,
                "response_group_id": "G",
                "channel": "speech",
                **({"places": places} if places else {}),
            },
        )
    finally:
        conn.close()
    append_record(host.memory, record_id=emitted.event_uid, source="jarvis", text=text)
    return emitted.event_uid


def logged_tool_call(  # noqa: PLR0913 - one keyword per thing a logged call can vary in
    host: StepsHost,
    turn_id: str,
    tool: str,
    arguments: object,
    *,
    take: float = 0.15,
    ending: str = "action.result_observed",
    error: str | None = None,
) -> None:
    """One tool call as the dispatcher logs it: proposed, running, then its terminal row."""
    conn = open_event_log(host.log)
    action = f"A-{tool}-{time.monotonic_ns()}"
    try:
        emit_event(
            conn,
            type="action.proposed",
            payload={
                "action_id": action,
                "tool_name": tool,
                "caller_principal": "jarvis_llm",
                "risk_level": "L1",
                "arguments": arguments,
                "turn_id": turn_id,
            },
            correlation={"action_id": action, "turn_id": turn_id},
        )
        running = emit_event(
            conn,
            type="action.running",
            payload={"action_id": action},
            correlation={"action_id": action, "turn_id": turn_id},
        )
        time.sleep(take)
        ended = {"action_id": action}
        if ending == "action.result_observed":
            ended |= {"semantics": "error" if error else "observation", "tool_output": "{}"}
        if error:
            ended["error"] = error
        emit_event(
            conn,
            type=ending,
            payload=ended,
            source_event_id=running.event_uid,
            correlation={"action_id": action},
        )
    finally:
        conn.close()


def frames_of(phone: FakePhone, kind: str) -> list[dict[str, Any]]:
    with phone.lock:
        return [f for f in phone.texts if f.get("type") == kind]


def history_of(host: StepsHost) -> list[dict[str, Any]]:
    """The history the model gets, with the line of what each answer did read from the log."""

    def did(ids: Sequence[str]) -> dict[str, str]:
        conn = open_event_log(host.log)
        try:
            return answer_did(conn, ids)
        finally:
            conn.close()

    return list(render_context(host.memory, exclude_id="", did=did).history)
