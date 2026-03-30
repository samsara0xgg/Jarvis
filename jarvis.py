"""Jarvis AI Voice Assistant — main entry point.

Supports two modes:
  - ``python jarvis.py``            → always-listening with wake word
  - ``python jarvis.py --no-wake``  → press-Enter-to-talk (no Porcupine needed)
"""

from __future__ import annotations

import argparse
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from auth.permission_manager import PermissionManager
from auth.user_store import UserStore
from core.audio_recorder import AudioRecorder
from core.automation_engine import AutomationEngine
from core.event_bus import EventBus
from core.llm import LLMClient
from core.speaker_encoder import SpeakerEncoder
from core.speaker_verifier import SpeakerVerifier
from core.speech_recognizer import SpeechRecognizer
from devices.device_manager import DeviceManager
from memory.conversation import ConversationStore
from memory.user_preferences import UserPreferenceStore
from skills import SkillRegistry
from skills.automation import AutomationSkill
from skills.memory_skill import MemorySkill
from skills.reminders import ReminderSkill
from skills.smart_home import SmartHomeSkill
from skills.system_control import SystemControlSkill
from skills.time_skill import TimeSkill
from skills.todos import TodoSkill
from skills.weather import WeatherSkill

LOGGER = logging.getLogger(__name__)

FAREWELL_DEFAULTS = ["再见", "退出", "bye", "goodbye", "that's all"]


class JarvisApp:
    """Orchestrate the full Jarvis voice assistant pipeline.

    Wires together wake-word detection, speaker verification, ASR,
    Claude LLM with tool calling, skill execution, and TTS output.
    """

    def __init__(self, config: dict, *, config_path: str | Path | None = None) -> None:
        self.config = config
        self.config_path = Path(config_path) if config_path else Path("config.yaml")
        self.logger = LOGGER

        # --- Event bus ---
        self.event_bus = EventBus()

        # --- Reuse existing modules ---
        self.user_store = UserStore(config)
        self.audio_recorder = AudioRecorder(config)
        self.speaker_encoder = SpeakerEncoder(config)
        self.speaker_verifier = SpeakerVerifier(config, self.speaker_encoder, self.user_store)
        self.speech_recognizer = SpeechRecognizer(config)
        self.device_manager = DeviceManager(config, event_bus=self.event_bus)
        self.permission_manager = PermissionManager()

        # --- New Jarvis modules ---
        self.llm = LLMClient(config)
        self.conversation_store = ConversationStore(config)
        self.preference_store = UserPreferenceStore(config)

        # --- Scheduler (optional) ---
        self.scheduler = None
        try:
            from core.scheduler import JarvisScheduler
            self.scheduler = JarvisScheduler(config, self.event_bus)
            if self.scheduler.available and config.get("scheduler", {}).get("enabled", True):
                self.scheduler.start()
                self._setup_morning_briefing(config)
        except Exception as exc:
            self.logger.warning("Scheduler unavailable: %s", exc)

        # --- Automation engine ---
        self.automation_engine = AutomationEngine(
            device_manager=self.device_manager,
            event_bus=self.event_bus,
            tts_callback=self.speak,
        )
        for scene_name, steps in config.get("automations", {}).items():
            if isinstance(steps, list):
                self.automation_engine.register_scene(scene_name, steps)

        # --- OLED display (optional) ---
        self.oled = None
        if config.get("oled", {}).get("enabled", False):
            try:
                from ui.oled_display import OledDisplay
                self.oled = OledDisplay(config, self.event_bus)
                self.oled.start()
            except Exception as exc:
                self.logger.warning("OLED display unavailable: %s", exc)

        # --- Skill registry ---
        self.skill_registry = SkillRegistry()
        self._register_skills(config)

        # --- Intent router + local executor (optional) ---
        self.intent_router = None
        self.local_executor = None
        try:
            from core.intent_router import IntentRouter
            from core.local_executor import LocalExecutor
            self.intent_router = IntentRouter(config)
            self.local_executor = LocalExecutor(self.skill_registry)
        except Exception as exc:
            self.logger.warning("Intent router unavailable: %s", exc)

        # --- TTS (lazy loaded) ---
        self._tts: Any = None

        # --- Session state ---
        session_config = config.get("session", {})
        self.silence_timeout = float(session_config.get("silence_timeout", 30))
        self.utterance_duration = float(session_config.get("utterance_duration", 5))
        self.farewell_phrases = set(
            str(p).strip().lower()
            for p in session_config.get("farewell_phrases", FAREWELL_DEFAULTS)
        )
        self._running = True
        self._last_interaction = time.monotonic()

    def shutdown(self) -> None:
        """Clean up all subsystems."""
        if self.scheduler and self.scheduler.available:
            self.scheduler.stop()
        if self.oled:
            self.oled.stop()
        if hasattr(self.device_manager, '_mqtt_client') and self.device_manager._mqtt_client:
            self.device_manager._mqtt_client.disconnect()

    def run_interactive(self) -> int:
        """Run in press-Enter-to-talk mode (no wake word needed).

        Returns:
            Process exit code.
        """
        self._print_banner()
        self.speak("Jarvis online. Awaiting your command.")

        try:
            while self._running:
                try:
                    user_input = input("\n[Press Enter to speak, 'quit' to exit] ").strip()
                except (EOFError, KeyboardInterrupt):
                    self.speak("Goodbye.")
                    return 0

                if user_input.lower() in {"quit", "exit", "退出", "q"}:
                    self.speak("Goodbye.")
                    return 0

                try:
                    audio = self.audio_recorder.record(self.utterance_duration)
                    response = self.handle_utterance(audio)
                    if response and self._is_farewell(response):
                        self.speak("Goodbye.")
                        return 0
                except KeyboardInterrupt:
                    print("\nRecording cancelled.")
                    continue
                except Exception as exc:
                    self.logger.exception("Pipeline error")
                    print(f"Error: {exc}")
                    continue
        finally:
            self.shutdown()

        return 0

    def run_always_listening(self) -> int:
        """Run with Porcupine wake word detection (always-on mode).

        Returns:
            Process exit code.
        """
        from core.wake_word import WakeWordDetector

        wake_config = self.config.get("wake_word", {})
        if not wake_config.get("picovoice_access_key"):
            self.logger.error("Picovoice access key not configured. Use --no-wake or set wake_word.picovoice_access_key.")
            print("Error: Picovoice access key not set. Get one at https://console.picovoice.ai/")
            print("Set it in config.yaml under wake_word.picovoice_access_key")
            print("Or run with --no-wake for press-Enter mode.")
            return 1

        detector = WakeWordDetector(self.config)
        self._print_banner()

        try:
            import sounddevice as sd
        except ImportError:
            self.logger.error("sounddevice is required for always-listening mode.")
            return 1

        detector.start()
        self.speak("Jarvis online. Say 'Hey Jarvis' to activate.")

        try:
            stream = sd.InputStream(
                samplerate=detector.sample_rate,
                channels=1,
                dtype="int16",
                blocksize=detector.frame_length,
            )
            stream.start()

            listening_for_wake = True
            self._last_interaction = time.monotonic()

            while self._running:
                if listening_for_wake:
                    frame, _ = stream.read(detector.frame_length)
                    pcm = frame[:, 0].tolist()
                    if detector.process_frame(pcm):
                        self.logger.info("Wake word detected!")
                        self.speak_short("Sir?")
                        listening_for_wake = False
                        self._last_interaction = time.monotonic()
                else:
                    # Active conversation mode
                    elapsed = time.monotonic() - self._last_interaction
                    if elapsed > self.silence_timeout:
                        self.logger.info("Session timeout after %.0fs silence.", elapsed)
                        listening_for_wake = True
                        continue

                    try:
                        # Pause the wake-word stream, record via AudioRecorder
                        stream.stop()
                        audio = self.audio_recorder.record(self.utterance_duration)
                        stream.start()

                        response = self.handle_utterance(audio)
                        self._last_interaction = time.monotonic()

                        if response and self._is_farewell(response):
                            listening_for_wake = True
                    except KeyboardInterrupt:
                        break
                    except Exception as exc:
                        self.logger.exception("Pipeline error in active session")
                        self.speak(f"Sorry, something went wrong: {exc}")
                        stream.start()

        except KeyboardInterrupt:
            pass
        finally:
            detector.stop()
            self.speak("Jarvis shutting down.")
            self.shutdown()

        return 0

    def handle_utterance(self, audio: np.ndarray) -> str:
        """Process one utterance through the full Jarvis pipeline.

        Args:
            audio: Recorded waveform.

        Returns:
            The assistant's text response.
        """
        try:
            return self._handle_utterance_inner(audio)
        except Exception:
            self.logger.exception("Utterance pipeline failed")
            self.event_bus.emit("jarvis.state_changed", {"state": "idle"})
            raise

    def _handle_utterance_inner(self, audio: np.ndarray) -> str:
        """Inner utterance handler — wrapped by handle_utterance for safety."""
        # 0. Signal listening
        self.event_bus.emit("jarvis.state_changed", {"state": "listening"})

        # 1. Parallel: speaker verification + ASR
        with ThreadPoolExecutor(max_workers=2) as executor:
            verify_future = executor.submit(self.speaker_verifier.verify, np.copy(audio))
            asr_future = executor.submit(self.speech_recognizer.transcribe, np.copy(audio))
            verification = verify_future.result()
            transcription = asr_future.result()

        text = transcription.text.strip()
        if not text:
            self.event_bus.emit("jarvis.state_changed", {"state": "idle"})
            self.speak("I didn't catch that. Could you repeat?")
            return ""

        # 2. Resolve user identity
        user_id = verification.user if verification.verified else None
        user_name = self._resolve_display_name(user_id)
        user_role = self._resolve_role(user_id)
        confidence = verification.confidence

        # 3. Log identification
        if user_id:
            self.logger.info(
                "Identified: %s (%.2f) said: %s", user_name, confidence, text,
            )
            print(f"🎤 {user_name} ({confidence:.2f}): {text}")
        else:
            self.logger.info("Unidentified speaker (%.2f) said: %s", confidence, text)
            print(f"🎤 Guest ({confidence:.2f}): {text}")

        # 4. Load conversation history
        session_id = user_id or "_guest"
        history = self.conversation_store.get_history(session_id)

        # 5. Route: local or cloud?
        self.event_bus.emit("jarvis.state_changed", {"state": "thinking"})
        response_text = None
        updated_messages = None

        if self.intent_router and self.local_executor:
            route = self.intent_router.route(text)
            if route.tier == "local":
                if route.intent == "smart_home":
                    error = self.local_executor.execute_smart_home(route.actions, user_role)
                    response_text = error or route.response
                elif route.intent == "info_query":
                    response_text = self.local_executor.execute_info_query(
                        route.sub_type, route.query, user_role,
                    )
                elif route.intent == "time":
                    response_text = self.local_executor.execute_time(route.sub_type)

        # 6. If local didn't handle it, fall back to cloud LLM
        if response_text is None:
            tools = self.skill_registry.get_tool_definitions(user_role)
            response_text, updated_messages = self.llm.chat(
                user_message=text,
                conversation_history=history,
                tools=tools,
                tool_executor=self.skill_registry.execute,
                user_name=user_name,
                user_id=user_id,
                user_role=user_role,
            )

        # 7. Save conversation
        if updated_messages is not None:
            self.conversation_store.replace(session_id, updated_messages)

        # 8. Output
        self.event_bus.emit("jarvis.state_changed", {"state": "speaking"})
        if self.oled:
            self.oled.set_speaking_text(response_text)
        print(f"🤖 Jarvis: {response_text}")
        self.speak(response_text)
        self.event_bus.emit("jarvis.state_changed", {"state": "idle"})

        return response_text

    def speak(self, text: str) -> None:
        """Speak text via TTS if available."""
        if not text:
            return
        tts = self._get_tts()
        if tts:
            try:
                tts.speak(text)
            except Exception as exc:
                self.logger.warning("TTS failed: %s", exc)

    def speak_short(self, text: str) -> None:
        """Speak a brief acknowledgment (low latency)."""
        tts = self._get_tts()
        if tts:
            try:
                tts.speak_short(text)
            except Exception as exc:
                self.logger.warning("TTS short failed: %s", exc)

    def _get_tts(self) -> Any:
        """Lazily initialize TTS engine."""
        if self._tts is not None:
            return self._tts
        try:
            from core.tts import TTSEngine
            self._tts = TTSEngine(self.config)
            return self._tts
        except Exception as exc:
            self.logger.warning("TTS unavailable: %s", exc)
            return None

    def _register_skills(self, config: dict) -> None:
        """Register all available skills."""
        self.skill_registry.register(
            SmartHomeSkill(self.device_manager, self.permission_manager)
        )
        self.skill_registry.register(WeatherSkill(config))
        self.skill_registry.register(TimeSkill(config))
        self.skill_registry.register(ReminderSkill(
            config,
            scheduler=self.scheduler,
            tts_callback=self.speak,
            event_bus=self.event_bus,
        ))
        self.skill_registry.register(TodoSkill(config))
        self.skill_registry.register(SystemControlSkill(config))
        self.skill_registry.register(MemorySkill(self.preference_store))
        self.skill_registry.register(AutomationSkill(self.automation_engine))

        # OpenClaw skill (optional)
        if config.get("skills", {}).get("realtime_data", {}).get("enabled", False):
            try:
                from skills.realtime_data import RealTimeDataSkill
                rt_skill = RealTimeDataSkill(config)
                self.skill_registry.register(rt_skill)
                if self.scheduler and self.scheduler.available:
                    rt_skill.set_scheduler(self.scheduler)
            except Exception as exc:
                self.logger.warning("RealTimeData skill unavailable: %s", exc)

        # Scheduler skill (optional)
        if self.scheduler and self.scheduler.available:
            try:
                from skills.scheduler_skill import SchedulerSkill
                self.skill_registry.register(SchedulerSkill(config, self.scheduler))
            except Exception as exc:
                self.logger.warning("Scheduler skill unavailable: %s", exc)

        # Remote control skill (optional)
        if config.get("remote", {}).get("enabled", False):
            try:
                from skills.remote_control import RemoteControlSkill
                self.skill_registry.register(RemoteControlSkill(config))
            except Exception as exc:
                self.logger.warning("Remote control skill unavailable: %s", exc)

        # Wire timer callbacks to TTS
        time_skill = self.skill_registry._skills.get("time")
        if time_skill and hasattr(time_skill, "set_timer_callback"):
            time_skill.set_timer_callback(self.speak)

        self.logger.info(
            "Registered %d skills: %s",
            len(self.skill_registry.skill_names),
            ", ".join(self.skill_registry.skill_names),
        )

    def _resolve_display_name(self, user_id: str | None) -> str | None:
        """Map user_id to display name."""
        if not user_id:
            return None
        record = self.user_store.get_user(user_id)
        if record:
            return str(record.get("name", user_id))
        return user_id

    def _resolve_role(self, user_id: str | None) -> str:
        """Map user_id to role."""
        if not user_id:
            return "guest"
        record = self.user_store.get_user(user_id)
        if record:
            return str(record.get("role", "guest"))
        return "guest"

    def _is_farewell(self, text: str) -> bool:
        """Check if text contains a farewell phrase."""
        normalized = text.strip().lower()
        return any(phrase in normalized for phrase in self.farewell_phrases)

    def _print_banner(self) -> None:
        """Print startup information."""
        mode = self.config.get("devices", {}).get("mode", "sim")
        user_count = len(self.user_store.get_all_users())
        skill_count = len(self.skill_registry.skill_names)
        print("=" * 60)
        print("  J.A.R.V.I.S. — Personal AI Voice Assistant")
        print("=" * 60)
        print(f"  Device mode : {mode}")
        print(f"  Users       : {user_count} registered")
        print(f"  Skills      : {skill_count} ({', '.join(self.skill_registry.skill_names)})")
        print(f"  LLM         : {self.llm.model}")
        print("=" * 60)

    def _setup_morning_briefing(self, config: dict) -> None:
        """Register the morning briefing cron job if configured."""
        briefing_cfg = config.get("scheduler", {}).get("morning_briefing", {})
        if not briefing_cfg.get("enabled", False):
            return
        cron_str = str(briefing_cfg.get("cron", "0 7 * * *"))
        parts = cron_str.split()
        if len(parts) != 5:
            self.logger.warning("Invalid morning briefing cron: %s", cron_str)
            return
        minute, hour, _, _, day_of_week = parts
        self.scheduler.add_cron_job(
            job_id="morning_briefing",
            func=self._run_morning_briefing,
            hour=hour,
            minute=minute,
            day_of_week=day_of_week,
        )
        self.logger.info("Morning briefing scheduled at %s:%s", hour, minute)

    def _run_morning_briefing(self) -> None:
        """Execute the morning briefing — weather + reminders + todos."""
        include = self.config.get("scheduler", {}).get("morning_briefing", {}).get("include", [])
        parts = ["Good morning. Here's your briefing."]

        if "weather" in include:
            weather_skill = self.skill_registry._skills.get("weather")
            if weather_skill:
                try:
                    result = weather_skill.execute("get_weather", {})
                    parts.append(result)
                except Exception as exc:
                    self.logger.warning("Briefing weather failed: %s", exc)

        if "reminders" in include:
            reminder_skill = self.skill_registry._skills.get("reminders")
            if reminder_skill:
                try:
                    result = reminder_skill.execute("list_reminders", {}, user_id="_briefing")
                    if "No active" not in result:
                        parts.append(result)
                except Exception as exc:
                    self.logger.warning("Briefing reminders failed: %s", exc)

        if "todos" in include:
            todo_skill = self.skill_registry._skills.get("todos")
            if todo_skill:
                try:
                    result = todo_skill.execute("list_todos", {}, user_id="_briefing")
                    if "No " not in result:
                        parts.append(result)
                except Exception as exc:
                    self.logger.warning("Briefing todos failed: %s", exc)

        if "realtime_data" in include:
            rt_skill = self.skill_registry._skills.get("realtime_data")
            if rt_skill:
                try:
                    result = rt_skill.get_briefing_text()
                    parts.append(result)
                except Exception as exc:
                    self.logger.warning("Briefing realtime_data failed: %s", exc)

        briefing_text = " ".join(parts)
        self.logger.info("Morning briefing: %s", briefing_text)
        self.speak(briefing_text)


def load_config(config_path: str | Path) -> dict[str, Any]:
    """Load YAML config from disk."""
    path = Path(config_path)
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge overlay into base. Overlay values win."""
    merged = dict(base)
    for key, value in overlay.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def configure_logging(config: dict) -> None:
    """Configure root logging."""
    level_name = str(config.get("logging", {}).get("level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def main() -> int:
    """CLI entry point for Jarvis."""
    parser = argparse.ArgumentParser(description="Jarvis AI Voice Assistant")
    parser.add_argument(
        "--no-wake",
        action="store_true",
        help="Disable wake word detection; use press-Enter-to-talk mode.",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml.",
    )
    parser.add_argument(
        "--config-overlay",
        default=None,
        help="Path to overlay YAML (e.g. deploy/pi.yaml). Deep-merged on top of base config.",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        config_path = Path(__file__).resolve().parent / "config.yaml"

    config = load_config(config_path)

    if args.config_overlay:
        overlay_path = Path(args.config_overlay)
        if overlay_path.exists():
            overlay = load_config(overlay_path)
            config = deep_merge(config, overlay)
            LOGGER.info("Applied config overlay from %s", overlay_path)

    configure_logging(config)

    try:
        app = JarvisApp(config, config_path=config_path)
    except Exception as exc:
        LOGGER.exception("Failed to initialize Jarvis.")
        print(f"Initialization failed: {exc}")
        return 1

    wake_enabled = config.get("wake_word", {}).get("enabled", True)
    if args.no_wake or not wake_enabled:
        return app.run_interactive()
    else:
        return app.run_always_listening()


if __name__ == "__main__":
    raise SystemExit(main())
