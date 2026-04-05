"""Jarvis AI Voice Assistant — main entry point.

Supports two modes:
  - ``python jarvis.py``            → always-listening with wake word
  - ``python jarvis.py --no-wake``  → press-Enter-to-talk (no Porcupine needed)
"""

from __future__ import annotations

import argparse
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from auth.permission_manager import PermissionManager
from core.local_executor import Action, ActionResponse
from core.tts import SentenceType
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
from memory.manager import MemoryManager
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
_REMEMBER_KEYWORDS = ("记住", "记下", "别忘了", "帮我记")

# Module-level ref so APScheduler can serialize the job.
_health_tracker_ref = None


def _run_health_probes() -> None:
    """Periodic health probe callback (must be module-level for APScheduler)."""
    if _health_tracker_ref is not None:
        _health_tracker_ref.run_all_probes()


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

        # --- Health tracker (optional) ---
        self.health_tracker = None
        if config.get("health", {}).get("enabled", True):
            try:
                from core.health import ComponentTracker
                self.health_tracker = ComponentTracker(config, event_bus=self.event_bus)
            except Exception as exc:
                self.logger.warning("Health tracker unavailable: %s", exc)

        # --- Reuse existing modules ---
        self.user_store = UserStore(config)
        self.audio_recorder = AudioRecorder(config)
        self.speaker_encoder = SpeakerEncoder(config)
        self.speaker_verifier = SpeakerVerifier(config, self.speaker_encoder, self.user_store)
        self.speech_recognizer = SpeechRecognizer(config)
        self.device_manager = DeviceManager(config, event_bus=self.event_bus)
        self.permission_manager = PermissionManager()

        # --- New Jarvis modules ---
        self.llm = LLMClient(config, tracker=self.health_tracker)
        self.conversation_store = ConversationStore(config)
        self.preference_store = UserPreferenceStore(config)

        # --- Memory manager ---
        self.memory_manager = MemoryManager(config)

        from memory.behavior_log import BehaviorLog
        mem_db = config.get("memory", {}).get("db_path", "data/memory/jarvis_memory.db")
        # BehaviorLog 和 MemoryManager 共用同一个 SQLite 文件（不同表），WAL 模式支持并发
        self.behavior_log = BehaviorLog(mem_db)

        from memory.direct_answer import DirectAnswerer
        self.direct_answerer = DirectAnswerer(
            self.memory_manager.store, self.memory_manager.embedder,
        )

        # Track last user/session for farewell save
        self._last_user_id: str | None = None
        self._last_session_id: str | None = None

        # --- Scheduler (optional) ---
        self.scheduler = None
        try:
            from core.scheduler import JarvisScheduler
            self.scheduler = JarvisScheduler(config, self.event_bus)
            if self.scheduler.available and config.get("scheduler", {}).get("enabled", True):
                self.scheduler.start()
                self._setup_morning_briefing(config)
                self._setup_memory_maintenance()
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

        # --- Intent router + local executor + automation rules ---
        self.intent_router = None
        self.local_executor = None
        self.rule_manager = None
        try:
            from core.intent_router import IntentRouter
            from core.local_executor import LocalExecutor
            from core.automation_rules import AutomationRuleManager

            self.intent_router = IntentRouter(config, tracker=self.health_tracker)

            # Automation rule manager — keyword 触发 + scheduler 注册
            def _execute_rule_actions(actions: list) -> None:
                """Callback for scheduled/keyword rule execution."""
                if self.local_executor:
                    ar = self.local_executor.execute_smart_home(actions, "owner")
                    if "失败" in ar.text:
                        self.logger.warning("Rule action failed: %s", ar.text)

            default_rules = str(Path(config_path).parent / "data" / "automation_rules.json") if config_path else "data/automation_rules.json"
            self.rule_manager = AutomationRuleManager(
                rules_path=config.get("automation", {}).get("rules_path", default_rules),
                scheduler=self.scheduler,
                action_executor=_execute_rule_actions,
            )

            self.local_executor = LocalExecutor(self.skill_registry, self.rule_manager)
        except Exception as exc:
            self.logger.warning("Intent router unavailable: %s", exc)

        # --- Learning router + skill factory ---
        from core.learning_router import LearningRouter
        from core.skill_factory import SkillFactory
        self.learning_router = LearningRouter(
            skill_names=list(self.skill_registry.skill_names),
        )
        self.skill_factory = SkillFactory(
            learned_dir="skills/learned",
            project_root=str(self.config_path.parent) if self.config_path else ".",
        )

        # --- TTS (lazy loaded) ---
        self._tts: Any = None

        # --- Session state ---
        self._cancel = threading.Event()  # 打断信号
        session_config = config.get("session", {})
        self.silence_timeout = float(session_config.get("silence_timeout", 30))
        self.utterance_duration = float(session_config.get("utterance_duration", 5))
        self.farewell_phrases = set(
            str(p).strip().lower()
            for p in session_config.get("farewell_phrases", FAREWELL_DEFAULTS)
        )
        self._running = True
        self._last_interaction = time.monotonic()
        self._executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="jarvis")
        self._tts_future: Future | None = None

        # 预热 embedding 模型（后台加载，不阻塞启动）
        self._executor.submit(self.memory_manager.embedder.encode, "warmup")

        # 预热 HTTP 连接（建立 keep-alive，首次真实调用省 ~100ms TCP+TLS）
        self._executor.submit(self._prewarm_connections)

        # --- Health monitoring (voice notification + proactive probes) ---
        if self.health_tracker:
            self.event_bus.on("health.status_changed", self._on_health_changed)
            self._setup_health_probes(config)

    def _prewarm_connections(self) -> None:
        """Pre-warm HTTP connections to reduce first-call latency."""
        import requests as _req
        if self.intent_router and self.intent_router.groq_key:
            try:
                _req.get(
                    "https://api.groq.com/openai/v1/models",
                    headers={"Authorization": f"Bearer {self.intent_router.groq_key}"},
                    timeout=5,
                )
            except Exception:
                pass

    def _on_health_changed(self, data: dict) -> None:
        """Voice-notify user on first degradation only."""
        if data.get("new_status") == "degraded" and data.get("old_status") == "healthy":
            component = data.get("component", "unknown")
            self.speak(f"{component} 暂时不可用，已切换备用。")

    def _setup_health_probes(self, config: dict) -> None:
        """Register health probes and schedule periodic checks."""
        import requests as _req

        tracker = self.health_tracker
        assert tracker is not None

        # API reachability probes (free — hit /models endpoint, no tokens)
        def _make_api_probe(url: str, key: str) -> callable:
            def probe() -> bool:
                if not key:
                    return False
                try:
                    resp = _req.get(
                        url,
                        headers={"Authorization": f"Bearer {key}"},
                        timeout=5,
                    )
                    return resp.status_code < 500
                except Exception:
                    return False
            return probe

        groq_cfg = config.get("models", {}).get("groq", {})
        groq_key = groq_cfg.get("api_key") or __import__("os").environ.get("GROQ_API_KEY", "")
        if groq_key:
            tracker.register_probe(
                "intent.groq",
                _make_api_probe("https://api.groq.com/openai/v1/models", groq_key),
            )

        openai_key = __import__("os").environ.get("OPENAI_API_KEY", "")
        if openai_key:
            tracker.register_probe(
                "tts.openai",
                _make_api_probe("https://api.openai.com/v1/models", openai_key),
            )

        # Local model file probe
        asr_model = Path(config.get("asr", {}).get(
            "sensevoice_model", "data/sensevoice-small-int8",
        ))
        if asr_model.exists():
            model_file = asr_model / "model.int8.onnx"
            tracker.register_probe("asr.sensevoice", lambda: model_file.exists())

        # Schedule periodic probes
        probe_cfg = config.get("health", {}).get("proactive_checks", {})
        if (
            probe_cfg.get("enabled", True)
            and self.scheduler
            and self.scheduler.available
        ):
            global _health_tracker_ref
            _health_tracker_ref = tracker
            interval = int(probe_cfg.get("interval_seconds", 60))
            self.scheduler.add_interval_job(
                job_id="health_probes",
                func=_run_health_probes,
                seconds=interval,
            )
            self.logger.info("Health probes scheduled every %ds", interval)

    def shutdown(self) -> None:
        """Clean up all subsystems."""
        self._executor.shutdown(wait=True)
        if self.scheduler and self.scheduler.available:
            self.scheduler.stop()
        if self.oled:
            self.oled.stop()
        if hasattr(self.device_manager, '_mqtt_client') and self.device_manager._mqtt_client:
            self.device_manager._mqtt_client.disconnect()

    def run_interactive(self) -> int:
        """Run in press-Enter-to-talk mode (no wake word needed).

        Press Enter to start recording. Press Enter again while Jarvis is
        processing/speaking to interrupt and start a new recording.

        Returns:
            Process exit code.
        """
        self._print_banner()
        self.speak("Jarvis 已上线，随时待命。")

        # 后台线程监听 Enter 键
        enter_pressed = threading.Event()
        input_value: list[str] = [""]
        input_done = threading.Event()  # 标记整个 input 循环结束

        def _input_listener() -> None:
            while not input_done.is_set():
                try:
                    val = input("\n[Press Enter to speak, Enter again to interrupt] ").strip()
                    input_value[0] = val
                    enter_pressed.set()
                except (EOFError, KeyboardInterrupt):
                    input_value[0] = "quit"
                    enter_pressed.set()
                    break

        listener = threading.Thread(target=_input_listener, daemon=True)
        listener.start()

        try:
            while self._running:
                # 等用户按 Enter
                enter_pressed.wait()
                enter_pressed.clear()

                val = input_value[0]
                if val.lower() in {"quit", "exit", "退出", "q"}:
                    self.speak("再见。")
                    return 0

                try:
                    audio = self.audio_recorder.record(self.utterance_duration)
                    self._cancel.clear()

                    # 在后台检测打断：如果处理过程中用户再按 Enter，设置 cancel
                    def _watch_interrupt() -> None:
                        enter_pressed.wait()
                        if not self._cancel.is_set():
                            self._cancel.set()
                            self.logger.info("User interrupted current operation")

                    watcher = threading.Thread(target=_watch_interrupt, daemon=True)
                    watcher.start()

                    response = self.handle_utterance(audio)

                    if self._cancel.is_set():
                        # 被打断了：停止 TTS，清理状态
                        self._cancel_current()
                        self._cancel.clear()
                        print("⏹ 已打断")
                        # enter_pressed 已经被 watcher 消费，不需要 clear
                        continue

                    if response and self._is_farewell(response):
                        self._save_on_farewell()
                        self.speak("再见。")
                        return 0

                except KeyboardInterrupt:
                    print("\nRecording cancelled.")
                    self._cancel.clear()
                    continue
                except Exception as exc:
                    self.logger.exception("Pipeline error")
                    print(f"Error: {exc}")
                    self._cancel.clear()
                    continue
                finally:
                    self._flush_stdin()
        finally:
            input_done.set()
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
        self.speak("Jarvis 已上线，说 Hey Jarvis 唤醒我。")

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
                        self.speak_short("在的。")
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
                            self._save_on_farewell()
                            listening_for_wake = True
                    except KeyboardInterrupt:
                        break
                    except Exception as exc:
                        self.logger.exception("Pipeline error in active session")
                        self.speak(f"抱歉，出了点问题：{exc}")
                        stream.start()

        except KeyboardInterrupt:
            pass
        finally:
            detector.stop()
            self.speak("Jarvis 关机中。")
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
        self._wait_tts()  # 确保上一轮 TTS 播完再处理新音频
        verify_future = self._executor.submit(self.speaker_verifier.verify, np.copy(audio))
        asr_future = self._executor.submit(self.speech_recognizer.transcribe, np.copy(audio))
        verification = verify_future.result()
        transcription = asr_future.result()

        text = transcription.text.strip()
        detected_emotion = getattr(transcription, "emotion", "") or ""
        if not text:
            self.event_bus.emit("jarvis.state_changed", {"state": "idle"})
            self.speak("没听清，能再说一遍吗？")
            return ""

        # 2. Resolve user identity
        user_id = verification.user if verification.verified else None
        # 没有注册用户时，默认当 owner 处理（开发/单用户模式）
        if user_id is None and not self.user_store.get_all_users():
            user_id = "default_user"
        user_name = self._resolve_display_name(user_id) or "用户"
        user_role = self._resolve_role(user_id) if user_id != "default_user" else "owner"
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
        self._last_user_id = user_id
        self._last_session_id = session_id
        history = self.conversation_store.get_history(session_id)

        # 4b. Fast local checks first (avoid wasted API calls)

        # Memory store shortcut: "记住/记下/别忘了" → 直接确认，不走 LLM
        if any(text.startswith(kw) or kw in text[:10] for kw in _REMEMBER_KEYWORDS):
            # 不含"每次"（那是配置型学习意图，交给 learning router）
            if "每次" not in text:
                reply = "好的，记住了。"
                self.logger.info("Memory shortcut: %s", text[:60])
                self.event_bus.emit("jarvis.state_changed", {"state": "speaking"})
                print(f"🤖 Jarvis: {reply}")
                self._speak_nonblocking(reply, emotion=detected_emotion)
                history.append({"role": "user", "content": text})
                history.append({"role": "assistant", "content": reply})
                self.conversation_store.replace(session_id, history)
                # 后台记忆提取会自动处理
                if user_id:
                    self._executor.submit(
                        self.memory_manager.save, history, user_id, session_id,
                        detected_emotion,
                    )
                return reply

        # Learning intent detection
        if hasattr(self, "learning_router"):
            learning = self.learning_router.detect(text)
            if learning and learning.mode == "create":
                self.logger.info("Learning intent: create — %s", learning.description[:60])
                learn_response = self._learn_create(learning, user_id)
                self.event_bus.emit("jarvis.state_changed", {"state": "speaking"})
                print(f"🤖 Jarvis: {learn_response}")
                self._speak_nonblocking(learn_response, emotion=detected_emotion)
                history.append({"role": "user", "content": text})
                history.append({"role": "assistant", "content": learn_response})
                self.conversation_store.replace(session_id, history)
                if user_id:
                    self.behavior_log.log(user_id, "conversation", {
                        "text": text[:100], "route": "learn_create",
                    })
                return learn_response
            # config and compose modes: fall through to cloud LLM
            # (cloud LLM handles via existing automation skill)

        # Keyword trigger check
        self.event_bus.emit("jarvis.state_changed", {"state": "thinking"})
        response_text = None
        updated_messages = None
        ar: ActionResponse | None = None
        sentence_count = 0
        use_llm_rephrase = False

        if self.rule_manager and self.local_executor:
            match = self.rule_manager.check_keyword(text)
            if match:
                keyword_actions, rule_name = match
                if keyword_actions and keyword_actions[0].get("skill"):
                    # skill_alias: call skill then let LLM rephrase
                    ar = self.local_executor.execute_skill_alias(
                        keyword_actions, user_role,
                    )
                    if ar.action == Action.REQLLM:
                        use_llm_rephrase = True
                    else:
                        response_text = ar.text
                else:
                    ar = self.local_executor.execute_smart_home(
                        keyword_actions, user_role, response=f"好的，{rule_name}已执行。",
                    )
                    response_text = ar.text

        # 4c. Launch parallel futures for route + memory (saves ~150ms)
        memory_context = ""
        route_future = None
        memory_future = None

        if response_text is None and self.intent_router and self.local_executor:
            route_future = self._executor.submit(self.intent_router.route, text)
            if user_id:
                memory_future = self._executor.submit(
                    self.memory_manager.query, text, user_id,
                )

        # Level 1: Try direct answer from memory while futures run
        if user_id and response_text is None:
            try:
                direct = self.direct_answerer.try_answer(text, user_id)
                if direct:
                    self.logger.info("Level 1 direct answer: %s", direct[:60])
                    self.event_bus.emit("jarvis.state_changed", {"state": "speaking"})
                    print(f"🤖 Jarvis (L1): {direct}")
                    self._speak_nonblocking(direct, emotion=detected_emotion)
                    self.behavior_log.log(user_id, "conversation", {
                        "text": text[:100],
                        "route": "memory_l1",
                        "answer": direct[:100],
                    })
                    return direct
            except Exception as exc:
                self.logger.warning("Level 1 answer failed: %s", exc)

        # 4d. Collect parallel results
        if memory_future:
            try:
                memory_context = memory_future.result(timeout=5)
            except Exception as exc:
                self.logger.warning("Memory query failed: %s", exc)

        # 5-6. Route: local or cloud?
        if route_future:
            try:
                route = route_future.result(timeout=8)
            except Exception as exc:
                self.logger.warning("Intent route failed: %s", exc)
                route = None
        else:
            route = None

        if response_text is None and route is not None:
            if route.tier == "local":
                if route.intent == "smart_home":
                    ar = self.local_executor.execute_smart_home(
                        route.actions, user_role, response=route.response,
                    )
                elif route.intent == "info_query":
                    # 非明确数据查询（非 news/stocks/weather）先查记忆
                    if route.sub_type not in ("news", "stocks", "weather") and user_id:
                        try:
                            mem_answer = self.direct_answerer.try_answer(text, user_id)
                            if mem_answer:
                                ar = ActionResponse(Action.RESPONSE, mem_answer)
                        except Exception:
                            pass
                    if ar is None:
                        ar = self.local_executor.execute_info_query(
                            route.sub_type, route.query, user_role,
                        )
                elif route.intent == "time":
                    ar = self.local_executor.execute_time(route.sub_type)
                elif route.intent == "automation":
                    ar = self.local_executor.execute_automation(
                        route.sub_type, route.rule,
                    )
                    # 如果 Groq 也给了 response，优先用它
                    if route.response is not None:
                        ar = type(ar)(ar.action, route.response)
                else:
                    ar = None

                if ar is not None:
                    if ar.action == Action.REQLLM:
                        use_llm_rephrase = True
                        response_text = None  # 下面交给 LLM 转述
                    elif any(p in ar.text for p in ("没查到", "未找到", "暂不支持")):
                        response_text = None  # 本地无结果，fallback 到云端 LLM
                    else:
                        response_text = ar.text

        # 7. Cloud LLM — either REQLLM rephrase or full cloud fallback
        if response_text is None and not self._cancel.is_set():
            tools = self.skill_registry.get_tool_definitions(user_role)

            # 逐句 TTS：用 pipeline 异步合成+播放，消除句间停顿
            tts_pipeline = self._create_tts_pipeline()

            def _on_sentence(sentence: str) -> None:
                if self._cancel.is_set():
                    return  # 被打断，跳过后续句子
                nonlocal sentence_count
                sentence_count += 1
                print(f"🤖 Jarvis: {sentence}")
                if tts_pipeline:
                    st = SentenceType.FIRST if sentence_count == 1 else SentenceType.MIDDLE
                    tts_pipeline.submit(sentence, st, emotion=detected_emotion)
                else:
                    self._wait_tts()
                    self._speak_nonblocking(sentence, emotion=detected_emotion)

            self.event_bus.emit("jarvis.state_changed", {"state": "speaking"})

            try:
                # REQLLM: 让 LLM 用小贾语气转述本地数据
                if use_llm_rephrase and ar is not None:
                    rephrase_msg = (
                        f"用户问的是：{text}\n"
                        f"以下是查到的信息，用你自己的话简短转述给用户：\n{ar.text}"
                    )
                    try:
                        response_text, updated_messages = self.llm.chat_stream(
                            user_message=rephrase_msg,
                            conversation_history=history,
                            user_name=user_name,
                            user_id=user_id,
                            user_role=user_role,
                            on_sentence=_on_sentence,
                            user_emotion=detected_emotion,
                            memory_context=memory_context,
                        )
                    except Exception as exc:
                        self.logger.error("LLM rephrase failed: %s", exc)
                        response_text = ar.text  # 降级：直接播报原始数据
                else:
                    try:
                        response_text, updated_messages = self.llm.chat_stream(
                            user_message=text,
                            conversation_history=history,
                            tools=tools,
                            tool_executor=self.skill_registry.execute,
                            user_name=user_name,
                            user_id=user_id,
                            user_role=user_role,
                            on_sentence=_on_sentence,
                            user_emotion=detected_emotion,
                            memory_context=memory_context,
                        )
                    except Exception as exc:
                        self.logger.error("Cloud LLM failed: %s", exc)
                        response_text = "抱歉，云端服务暂时不可用。请检查 API key 配置。"
            finally:
                # 确保 pipeline 线程被清理，无论是否有异常
                if tts_pipeline:
                    if sentence_count > 0:
                        tts_pipeline.finish()
                        tts_pipeline.wait_done()
                    tts_pipeline.stop()

        # 8. Save conversation + memory extraction
        cloud_path = updated_messages is not None
        if cloud_path:
            # 云端 LLM 路径：完整对话历史 + 立即提取记忆
            self.conversation_store.replace(session_id, updated_messages)
            if user_id:
                self._executor.submit(
                    self.memory_manager.save, updated_messages, user_id, session_id,
                    detected_emotion,
                )
        elif response_text:
            # 本地路径：更新对话历史（farewell/超时时统一提取记忆）
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": response_text})
            self.conversation_store.replace(session_id, history)
            updated_messages = history  # 给后续行为日志用

        # 9. Log behavior events
        if user_id:
            if updated_messages:
                for msg in updated_messages:
                    if msg.get("role") == "assistant" and isinstance(msg.get("content"), list):
                        for block in msg["content"]:
                            if isinstance(block, dict) and block.get("type") == "tool_use":
                                self.behavior_log.log(user_id, "skill_call", {
                                    "skill": block.get("name", ""),
                                    "input": block.get("input", {}),
                                })
            self.behavior_log.log(user_id, "conversation", {
                "text": text[:100],
                "emotion": detected_emotion,
                "route": "local" if response_text and not updated_messages else "cloud",
            })

        # 10. Output (only for non-streamed responses)
        if sentence_count == 0 and not self._cancel.is_set():
            self.event_bus.emit("jarvis.state_changed", {"state": "speaking"})
            if self.oled:
                self.oled.set_speaking_text(response_text)
            print(f"🤖 Jarvis: {response_text}")
            self._speak_nonblocking(response_text, emotion=detected_emotion)

        return response_text

    # ------------------------------------------------------------------
    # Text-only pipeline (for web frontend — no audio, no TTS)
    # ------------------------------------------------------------------
    def handle_text(
        self,
        text: str,
        session_id: str = "_web",
        on_sentence: Any = None,
        emotion: str = "",
    ) -> str:
        """Process a text message without audio/TTS.

        Reuses steps 4-9 of ``_handle_utterance_inner`` but skips
        recording, ASR, voiceprint verification, TTS playback, and
        event-bus emissions.  The caller (web server) is responsible
        for converting the response to speech separately.

        Args:
            text: User message.
            session_id: Conversation session identifier.
            on_sentence: Optional callback ``fn(sentence, emotion='')``.
            emotion: Detected emotion label (passed through to callback).

        Returns:
            Full assistant response text.
        """
        user_id = "default_user"
        user_name = "用户"
        user_role = "owner"

        # 4. Load conversation history + memory
        history = self.conversation_store.get_history(session_id)
        memory_context = ""
        try:
            memory_context = self.memory_manager.query(text, user_id)
        except Exception as exc:
            self.logger.warning("Memory query failed: %s", exc)

        # 4b. Level 1: direct answer from memory
        try:
            direct = self.direct_answerer.try_answer(text, user_id)
            if direct:
                self.logger.info("Level 1 direct answer: %s", direct[:60])
                if on_sentence:
                    on_sentence(direct, emotion=emotion)
                return direct
        except Exception as exc:
            self.logger.warning("Level 1 answer failed: %s", exc)

        # 4c. Memory store shortcut
        if any(text.startswith(kw) or kw in text[:10] for kw in _REMEMBER_KEYWORDS):
            if "每次" not in text:
                reply = "好的，记住了。"
                self.logger.info("Memory shortcut: %s", text[:60])
                history.append({"role": "user", "content": text})
                history.append({"role": "assistant", "content": reply})
                self.conversation_store.replace(session_id, history)
                self._executor.submit(
                    self.memory_manager.save, history, user_id, session_id,
                    emotion,
                )
                if on_sentence:
                    on_sentence(reply, emotion=emotion)
                return reply

        # 4d. Learning intent detection
        if hasattr(self, "learning_router"):
            learning = self.learning_router.detect(text)
            if learning and learning.mode == "create":
                self.logger.info("Learning intent: create — %s", learning.description[:60])
                learn_response = self._learn_create(learning, user_id)
                history.append({"role": "user", "content": text})
                history.append({"role": "assistant", "content": learn_response})
                self.conversation_store.replace(session_id, history)
                if on_sentence:
                    on_sentence(learn_response, emotion=emotion)
                return learn_response

        # 5. Keyword trigger check
        response_text = None
        updated_messages = None
        ar: ActionResponse | None = None
        sentence_count = 0
        use_llm_rephrase = False

        if self.rule_manager and self.local_executor:
            match = self.rule_manager.check_keyword(text)
            if match:
                keyword_actions, rule_name = match
                if keyword_actions and keyword_actions[0].get("skill"):
                    ar = self.local_executor.execute_skill_alias(
                        keyword_actions, user_role,
                    )
                    if ar.action == Action.REQLLM:
                        use_llm_rephrase = True
                    else:
                        response_text = ar.text
                else:
                    ar = self.local_executor.execute_smart_home(
                        keyword_actions, user_role, response=f"好的，{rule_name}已执行。",
                    )
                    response_text = ar.text

        # 6. Route: local or cloud?
        if response_text is None and self.intent_router and self.local_executor:
            route = self.intent_router.route(text)
            if route.tier == "local":
                if route.intent == "smart_home":
                    ar = self.local_executor.execute_smart_home(
                        route.actions, user_role, response=route.response,
                    )
                elif route.intent == "info_query":
                    if route.sub_type not in ("news", "stocks", "weather"):
                        try:
                            mem_answer = self.direct_answerer.try_answer(text, user_id)
                            if mem_answer:
                                ar = ActionResponse(Action.RESPONSE, mem_answer)
                        except Exception:
                            pass
                    if ar is None:
                        ar = self.local_executor.execute_info_query(
                            route.sub_type, route.query, user_role,
                        )
                elif route.intent == "time":
                    ar = self.local_executor.execute_time(route.sub_type)
                elif route.intent == "automation":
                    ar = self.local_executor.execute_automation(
                        route.sub_type, route.rule,
                    )
                    if route.response is not None:
                        ar = type(ar)(ar.action, route.response)
                else:
                    ar = None

                if ar is not None:
                    if ar.action == Action.REQLLM:
                        use_llm_rephrase = True
                        response_text = None
                    elif any(p in ar.text for p in ("没查到", "未找到", "暂不支持")):
                        response_text = None
                    else:
                        response_text = ar.text

        # 7. Cloud LLM
        if response_text is None:
            tools = self.skill_registry.get_tool_definitions(user_role)

            def _on_sentence(sentence: str) -> None:
                nonlocal sentence_count
                sentence_count += 1
                if on_sentence:
                    on_sentence(sentence, emotion=emotion)

            try:
                if use_llm_rephrase and ar is not None:
                    rephrase_msg = (
                        f"用户问的是：{text}\n"
                        f"以下是查到的信息，用你自己的话简短转述给用户：\n{ar.text}"
                    )
                    try:
                        response_text, updated_messages = self.llm.chat_stream(
                            user_message=rephrase_msg,
                            conversation_history=history,
                            user_name=user_name,
                            user_id=user_id,
                            user_role=user_role,
                            on_sentence=_on_sentence,
                            user_emotion=emotion,
                            memory_context=memory_context,
                        )
                    except Exception as exc:
                        self.logger.error("LLM rephrase failed: %s", exc)
                        response_text = ar.text
                else:
                    try:
                        response_text, updated_messages = self.llm.chat_stream(
                            user_message=text,
                            conversation_history=history,
                            tools=tools,
                            tool_executor=self.skill_registry.execute,
                            user_name=user_name,
                            user_id=user_id,
                            user_role=user_role,
                            on_sentence=_on_sentence,
                            user_emotion=emotion,
                            memory_context=memory_context,
                        )
                    except Exception as exc:
                        self.logger.error("Cloud LLM failed: %s", exc)
                        response_text = "抱歉，云端服务暂时不可用。请检查 API key 配置。"
            except Exception:
                pass  # outer safety net

        # 8. Save conversation + memory extraction
        cloud_path = updated_messages is not None
        if cloud_path:
            self.conversation_store.replace(session_id, updated_messages)
            self._executor.submit(
                self.memory_manager.save, updated_messages, user_id, session_id,
                emotion,
            )
        elif response_text:
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": response_text})
            self.conversation_store.replace(session_id, history)

        # 9. For non-streamed local responses, fire callback once
        if sentence_count == 0 and on_sentence and response_text:
            on_sentence(response_text, emotion=emotion)

        return response_text

    def speak(self, text: str) -> None:
        """Speak text via TTS (blocking). Use for non-hot-path calls."""
        if not text:
            return
        self._wait_tts()
        tts = self._get_tts()
        if tts:
            try:
                tts.speak(text)
            except Exception as exc:
                self.logger.warning("TTS failed: %s", exc)

    def _speak_nonblocking(self, text: str, emotion: str = "") -> None:
        """Speak text in a background thread (non-blocking hot path)."""
        if not text:
            self.event_bus.emit("jarvis.state_changed", {"state": "idle"})
            return
        tts = self._get_tts()
        if not tts:
            self.event_bus.emit("jarvis.state_changed", {"state": "idle"})
            return

        def _do_speak() -> None:
            try:
                tts.speak(text, emotion=emotion)
            except Exception as exc:
                self.logger.warning("TTS failed: %s", exc)
            finally:
                self.event_bus.emit("jarvis.state_changed", {"state": "idle"})

        self._tts_future = self._executor.submit(_do_speak)

    def _wait_tts(self) -> None:
        """Block until previous TTS finishes (prevents audio feedback)."""
        if self._tts_future and not self._tts_future.done():
            self._tts_future.result(timeout=30)

    def _cancel_current(self) -> None:
        """Cancel current TTS and reset state after user interrupt."""
        # 取消正在播放的 TTS
        if self._tts_future and not self._tts_future.done():
            self._tts_future.cancel()
        # 停止音频播放（如果 TTS 引擎支持）
        tts = self._get_tts()
        if tts and hasattr(tts, "stop"):
            try:
                tts.stop()
            except Exception:
                pass
        # 终止 SkillFactory 子进程
        if hasattr(self, "skill_factory"):
            try:
                self.skill_factory.cancel()
            except Exception:
                pass
        self.event_bus.emit("jarvis.state_changed", {"state": "idle"})

    @staticmethod
    def _flush_stdin() -> None:
        """Drain any buffered stdin input (extra Enter presses during recording)."""
        import sys
        import select
        try:
            while select.select([sys.stdin], [], [], 0.0)[0]:
                sys.stdin.readline()
        except (OSError, ValueError):
            pass

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
            self._tts = TTSEngine(self.config, tracker=self.health_tracker)
            return self._tts
        except Exception as exc:
            self.logger.warning("TTS unavailable: %s", exc)
            return None

    def _create_tts_pipeline(self) -> Any:
        """Create a new TTS pipeline for streaming output."""
        tts = self._get_tts()
        if not tts:
            return None
        try:
            from core.tts import TTSPipeline
            pipeline = TTSPipeline(tts)
            pipeline.start()
            return pipeline
        except Exception as exc:
            self.logger.warning("TTS pipeline unavailable: %s", exc)
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
        self.skill_registry.register(MemorySkill(self.memory_manager))
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

        # Health skill (optional)
        if self.health_tracker:
            try:
                from skills.health_skill import HealthSkill
                self.skill_registry.register(HealthSkill(self.health_tracker))
            except Exception as exc:
                self.logger.warning("Health skill unavailable: %s", exc)

        # --- Load learned skills ---
        from core.skill_loader import SkillLoader
        self.skill_loader = SkillLoader("skills/learned")
        for skill in self.skill_loader.scan():
            try:
                self.skill_registry.register(skill)
            except Exception as exc:
                self.logger.warning("Failed to register learned skill %s: %s", skill.skill_name, exc)

        # --- Skill management ---
        from skills.skill_mgmt import SkillManagementSkill
        self.skill_registry.register(SkillManagementSkill(self.skill_loader, self.skill_registry))

        # Wire timer callbacks to TTS
        time_skill = self.skill_registry._skills.get("time")
        if time_skill and hasattr(time_skill, "set_timer_callback"):
            time_skill.set_timer_callback(self.speak)

        self.logger.info(
            "Registered %d skills: %s",
            len(self.skill_registry.skill_names),
            ", ".join(self.skill_registry.skill_names),
        )

    def _learn_create(self, intent: Any, user_id: str | None) -> str:
        """创造型：调用 Claude Code 技能工厂。"""
        self.speak("好的，我去学一下，稍等。")

        result = self.skill_factory.create(
            description=intent.description,
            on_status=lambda msg: self.logger.info("SkillFactory: %s", msg),
        )

        if result["success"]:
            try:
                new_skills = self.skill_loader.scan()
                for skill in new_skills:
                    if skill.skill_name not in self.skill_registry.skill_names:
                        self.skill_registry.register(skill)
                        self.skill_loader.update_metadata(skill.skill_name, {
                            "taught_by": user_id or "unknown",
                            "description": intent.description,
                        })
                self.learning_router.update_skills(list(self.skill_registry.skill_names))
            except Exception as exc:
                self.logger.warning("Failed to hot-load new skill: %s", exc)
                return f"技能文件生成了但加载失败：{exc}"

            if user_id:
                self.behavior_log.log(user_id, "skill_learned", {
                    "skill": result["skill_name"],
                    "description": intent.description,
                })
            return f"学会了！现在我可以{intent.description}了，要试试吗？"
        else:
            return f"没学会，{result['message']}"

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

    def _save_on_farewell(self) -> None:
        """Save full conversation to memory on farewell."""
        user_id = self._last_user_id
        session_id = self._last_session_id
        if user_id and session_id:
            full_history = self.conversation_store.get_history(session_id)
            if full_history:
                self._executor.submit(
                    self.memory_manager.save, full_history, user_id, session_id,
                )

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

    def _setup_memory_maintenance(self) -> None:
        """Register daily memory maintenance (3am)."""
        self.scheduler.add_cron_job(
            job_id="memory_maintenance",
            func=self._run_memory_maintenance,
            hour="3",
            minute="0",
        )
        self.logger.info("Memory maintenance scheduled: daily 3:00am")

    def _run_memory_maintenance(self) -> None:
        """Execute daily memory maintenance — merge duplicates."""
        try:
            results = self.memory_manager.maintain_all()
            for uid, stats in results.items():
                if isinstance(stats, dict) and not stats.get("error"):
                    self.logger.info(
                        "Memory maintenance [%s]: merged=%d, checked=%d",
                        uid, stats.get("merged", 0), stats.get("checked", 0),
                    )
        except Exception:
            self.logger.exception("Memory maintenance failed")

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
