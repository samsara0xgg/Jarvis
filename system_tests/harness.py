"""Test harness — wraps JarvisApp with state management for system tests."""
from __future__ import annotations

import copy
import logging
import sys
import time
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from system_tests.assertions import evaluate
from system_tests.models import (
    DeviceChange,
    MemoryChange,
    MemoryDiff,
    ScenarioResult,
    StepExpect,
    StepResult,
    TtsInfo,
)

LOGGER = logging.getLogger(__name__)

# Fields in device status that are metadata, not controllable state
_DEVICE_META_FIELDS = {"device_id", "name", "device_type", "required_role", "is_available",
                       "color_temp_map", "color_xy"}


def diff_devices(
    before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]],
) -> list[DeviceChange]:
    """Compare two device snapshots, return list of field changes."""
    changes: list[DeviceChange] = []
    for device_id in after:
        b = before.get(device_id, {})
        a = after[device_id]
        for field_name, after_val in a.items():
            if field_name in _DEVICE_META_FIELDS:
                continue
            before_val = b.get(field_name)
            if before_val != after_val:
                changes.append(DeviceChange(device_id, field_name, before_val, after_val))
    return changes


def diff_memory(
    before: list[dict[str, Any]], after: list[dict[str, Any]],
) -> MemoryDiff:
    """Compare two memory snapshots by id, return added/removed."""
    before_ids = {m["id"]: m for m in before}
    after_ids = {m["id"]: m for m in after}
    added = [
        MemoryChange("added", m["content"], m.get("category"), m.get("key"))
        for mid, m in after_ids.items() if mid not in before_ids
    ]
    removed = [
        MemoryChange("removed", m["content"], m.get("category"), m.get("key"))
        for mid, m in before_ids.items() if mid not in after_ids
    ]
    return MemoryDiff(added=added, removed=removed)


class TestHarness:
    """Manages JarvisApp lifecycle and per-step state observation."""

    def __init__(self, tmp_dir: Path | None = None, *, live: bool = False, tts: bool = False) -> None:
        self._tmp_dir = tmp_dir or Path("/tmp/jarvis_system_test")
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        self._api_counter: dict[str, int] = {}
        self._live = live
        self._tts = tts
        self.app = self._create_app()

    def _build_config(self) -> dict:
        """Build a config dict for system testing: sim devices, temp DBs, real APIs."""
        import os
        return {
            "audio": {
                "sample_rate": 16000, "channels": 1, "default_duration": 1.0,
                "min_duration": 0.1, "low_volume_threshold": 0.001,
            },
            "asr": {"model_size": "base", "language": "zh"},
            "speaker": {"model_source": "test", "embedding_dim": 192, "device": "cpu"},
            "verification": {"threshold": 0.70},
            "enrollment": {"num_samples": 3, "default_role": "resident"},
            "auth": {"user_store_path": str(self._tmp_dir / "users.json")},
            "devices": {
                "mode": "sim",
                "sim_devices": [
                    {
                        "device_id": "bedroom_light", "name": "卧室灯",
                        "device_type": "light", "required_role": "guest",
                        "is_available": True,
                        "initial_state": {
                            "is_on": False, "brightness": 100,
                            "color_temp": "neutral", "color": "white",
                        },
                    },
                    {
                        "device_id": "living_room_light", "name": "客厅灯",
                        "device_type": "light", "required_role": "guest",
                        "is_available": True,
                        "initial_state": {
                            "is_on": False, "brightness": 100,
                            "color_temp": "neutral", "color": "white",
                        },
                    },
                    {
                        "device_id": "home_thermostat", "name": "客厅空调",
                        "device_type": "thermostat", "required_role": "member",
                        "is_available": True,
                        "initial_state": {"is_on": False, "temperature": 24},
                    },
                    {
                        "device_id": "front_door_lock", "name": "入户门锁",
                        "device_type": "door_lock", "required_role": "admin",
                        "is_available": True,
                        "initial_state": {"is_locked": True},
                    },
                ],
            },
            "hue": {
                "light_aliases": {
                    "bedroom_light": ["卧室灯", "卧室的灯"],
                    "living_room_light": ["客厅灯", "客厅的灯"],
                },
                "group_aliases": {},
                "scene_aliases": {},
                "voice_shortcuts": {},
            },
            "models": {
                "groq": {
                    "api_key": os.environ.get("GROQ_API_KEY", ""),
                    "model": "llama-3.3-70b-versatile",
                },
                "cerebras": {
                    "api_key": os.environ.get("CEREBRAS_API_KEY", ""),
                    "model": "llama-3.3-70b",
                },
            },
            "llm": {
                "provider": "xai",
                "presets": {
                    "fast": {
                        "provider": "xai",
                        "model": os.environ.get("XAI_MODEL", "grok-3-mini-fast"),
                        "api_key": os.environ.get("XAI_API_KEY", ""),
                    },
                },
                "default_preset": "fast",
                "max_tokens": 1024,
            },
            "tts": {"engine": "pyttsx3", "fallback_enabled": False},
            "wake_word": {"enabled": False},
            "session": {
                "silence_timeout": 30, "utterance_duration": 3,
                "farewell_phrases": ["再见", "退出", "bye", "goodbye"],
            },
            "memory": {
                "max_conversation_turns": 10,
                "db_path": str(self._tmp_dir / "memory.db"),
                "conversation_dir": str(self._tmp_dir / "convos"),
                "preferences_dir": str(self._tmp_dir / "prefs"),
            },
            "skills": {
                "weather": {"default_city": "Toronto"},
                "reminders": {"path": str(self._tmp_dir / "reminders.json")},
                "todos": {"dir": str(self._tmp_dir / "todos")},
            },
            "health": {"enabled": False},
            "logging": {"level": "WARNING"},
        }

    def _load_real_config(self) -> tuple[dict, Path]:
        """Load the real config.yaml for live mode."""
        config_path = Path("config.yaml")
        if not config_path.exists():
            config_path = Path(__file__).resolve().parent.parent / "config.yaml"
        with open(config_path, encoding="utf-8") as f:
            import yaml
            config = yaml.safe_load(f)
        # Override memory/conversation paths to temp dir so tests don't pollute real data
        config.setdefault("memory", {})
        config["memory"]["db_path"] = str(self._tmp_dir / "memory.db")
        config["memory"]["conversation_dir"] = str(self._tmp_dir / "convos")
        config["memory"]["preferences_dir"] = str(self._tmp_dir / "prefs")
        config.setdefault("auth", {})
        config["auth"]["user_store_path"] = str(self._tmp_dir / "users.json")
        # Disable wake word
        config.setdefault("wake_word", {})
        config["wake_word"]["enabled"] = False
        config.setdefault("health", {})
        config["health"]["enabled"] = False
        # If not wanting TTS, force pyttsx3 (silent)
        if not self._tts:
            config.setdefault("tts", {})
            config["tts"]["engine"] = "pyttsx3"
            config["tts"]["fallback_enabled"] = False
        return config, config_path

    def _create_app(self) -> Any:
        """Create JarvisApp with real APIs but mocked audio hardware."""
        from unittest.mock import patch

        if self._live:
            config, config_path = self._load_real_config()
        else:
            config = self._build_config()
            config_path = self._tmp_dir / "config.yaml"

        # Install fake pyttsx3 if not available and not using real TTS
        if not self._tts and "pyttsx3" not in sys.modules:
            fake_pyttsx3 = types.ModuleType("pyttsx3")
            mock_engine = MagicMock()
            fake_pyttsx3.init = MagicMock(return_value=mock_engine)
            sys.modules["pyttsx3"] = fake_pyttsx3

        with (
            patch("core.speaker_encoder.SpeakerEncoder"),
            patch("core.speaker_verifier.SpeakerVerifier"),
            patch("core.speech_recognizer.SpeechRecognizer"),
            patch("core.audio_recorder.AudioRecorder"),
        ):
            from jarvis import JarvisApp
            app = JarvisApp(config, config_path=config_path)

        # Wrap API methods for call counting
        self._wrap_api_counter(app)
        return app

    def _wrap_api_counter(self, app: Any) -> None:
        """Wrap API-calling methods to count calls per provider."""
        if app.intent_router:
            orig_route = app.intent_router.route_and_respond
            def _counted_route(*a: Any, **kw: Any) -> Any:
                self._api_counter["groq"] = self._api_counter.get("groq", 0) + 1
                return orig_route(*a, **kw)
            app.intent_router.route_and_respond = _counted_route

        orig_chat = app.llm.chat_stream
        def _counted_chat(*a: Any, **kw: Any) -> Any:
            self._api_counter["xai"] = self._api_counter.get("xai", 0) + 1
            return orig_chat(*a, **kw)
        app.llm.chat_stream = _counted_chat

        orig_save = app.memory_manager.save
        def _counted_save(*a: Any, **kw: Any) -> Any:
            self._api_counter["gpt4o_mini"] = self._api_counter.get("gpt4o_mini", 0) + 1
            return orig_save(*a, **kw)
        app.memory_manager.save = _counted_save

    def snapshot_devices(self) -> dict[str, dict[str, Any]]:
        return copy.deepcopy(self.app.device_manager.get_all_status())

    def snapshot_memory(self, user_id: str) -> list[dict[str, Any]]:
        memories = self.app.memory_manager.store.get_active_memories(user_id)
        # Strip embedding arrays for comparison (large, not useful for diff)
        for m in memories:
            m.pop("embedding", None)
        return memories

    def flush_background(self) -> None:
        """Wait for all background tasks (memory extraction etc.) to complete."""
        sentinel = self.app._executor.submit(lambda: None)
        sentinel.result(timeout=60)

    def reset_devices(self, setup: dict[str, dict[str, Any]] | None = None) -> None:
        """Reset all sim devices to initial or specified state."""
        for device_id, device in self.app.device_manager._devices.items():
            status = device.get_status()
            # Turn off everything first
            if status.get("is_on"):
                device.execute("turn_off")
            if status.get("is_locked") is False:
                device.execute("lock")
            # Apply setup overrides
            if setup and device_id in setup:
                for field_name, val in setup[device_id].items():
                    if field_name == "is_on" and val:
                        device.execute("turn_on")
                    elif field_name == "brightness":
                        device.execute("set_brightness", val)
                    elif field_name == "color_temp":
                        device.execute("set_color_temp", val)
                    elif field_name == "color" and val != "white":
                        device.execute("set_color", val)
                    elif field_name == "temperature":
                        device.execute("set_temperature", val)
                    elif field_name == "is_locked" and not val:
                        device.execute("unlock")

    def reset_memory(self, user_id: str) -> None:
        """Clear all memories for a user."""
        conn = self.app.memory_manager.store._get_conn()
        conn.execute("DELETE FROM memories WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM user_profiles WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM episodes WHERE user_id = ?", (user_id,))
        conn.commit()

    def reset_conversation(self, session_id: str) -> None:
        """Clear conversation history for a session."""
        self.app.conversation_store.replace(session_id, [])

    def reset_api_counter(self) -> None:
        self._api_counter.clear()

    def run_step(
        self,
        text: str,
        session_id: str,
        user_id: str = "default_user",
        user_name: str = "Allen",
        user_role: str = "owner",
        expect: StepExpect | None = None,
    ) -> StepResult:
        """Execute one step: snapshot -> handle_text -> flush -> diff -> assert."""
        before_devices = self.snapshot_devices()
        before_memory = self.snapshot_memory(user_id)
        self.reset_api_counter()

        sentences: list[str] = []

        def _on_sentence(sentence: str, **kw: Any) -> None:
            sentences.append(sentence)

        t0 = time.monotonic()
        error = None
        response = ""
        try:
            response = self.app.handle_text(
                text,
                session_id=session_id,
                on_sentence=_on_sentence,
                user_id=user_id,
                user_name=user_name,
                user_role=user_role,
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            LOGGER.exception("Step failed: %s", text)
        latency_ms = int((time.monotonic() - t0) * 1000)

        # Wait for background tasks
        try:
            self.flush_background()
        except Exception:
            pass

        after_devices = self.snapshot_devices()
        after_memory = self.snapshot_memory(user_id)

        device_changes = diff_devices(before_devices, after_devices)
        memory_diff_result = diff_memory(before_memory, after_memory)

        step = StepResult(
            input_text=text,
            response=response or "",
            sentences=sentences,
            route=getattr(self.app, "_last_route", None),
            path=getattr(self.app, "_last_path", None),
            device_changes=device_changes,
            memory_diff=memory_diff_result,
            latency_ms=latency_ms,
            api_calls=dict(self._api_counter),
            assertions={},
            error=error,
        )

        # Evaluate assertions
        if expect:
            step.assertions = evaluate(step, expect, current_device_state=after_devices)

        # TTS: capture info and optionally play
        tts_info = None
        if response and response != "farewell":
            tts = self.app._get_tts()
            engine_name = "none"
            if tts:
                engine_name = getattr(tts, '_current_engine', None) or tts.config.get("engine", "unknown")
            emotion_label = getattr(self.app, "_last_route", None)
            emotion_str = ""
            if emotion_label and hasattr(emotion_label, "response"):
                emotion_str = ""  # emotion comes from ASR, not available in text mode

            if self._tts and tts:
                t_tts = time.monotonic()
                try:
                    tts.speak(response)
                except Exception:
                    pass
                synth_ms = int((time.monotonic() - t_tts) * 1000)
                tts_info = TtsInfo(engine=engine_name, emotion="", synth_ms=synth_ms, played=True)
            else:
                # Just report what TTS would be used, no playback
                tts_info = TtsInfo(engine=engine_name, emotion="", synth_ms=0, played=False)

        step.tts_info = tts_info
        return step

    def shutdown(self) -> None:
        try:
            self.app.shutdown()
        except Exception:
            pass
