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
from typing import Any, Callable

import numpy as np
import urllib3
import yaml

# 压制第三方库的无用警告，保持日志干净
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import os
import warnings
warnings.filterwarnings("ignore", message=".*CUDAExecutionProvider.*")
os.environ["ONNXRUNTIME_LOG_SEVERITY_LEVEL"] = "3"  # onnxruntime 在没有 GPU 时会刷屏警告

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

# 用户说这些词就结束对话，回到待命/唤醒词监听
FAREWELL_DEFAULTS = ["再见", "退出", "bye", "goodbye", "that's all"]
# 用户说这些词就跳过 LLM，直接确认"记住了"，后台异步提取记忆
_REMEMBER_KEYWORDS = ("记住", "记下", "别忘了", "帮我记")
# 用户说这些前缀词，本轮临时切换到更强的 LLM 模型（deep preset）
_ESCALATION_KEYWORDS = ("仔细想想", "详细分析", "认真想", "好好想")
# 这些智能家居动作无法本地解析参数，必须交给 LLM 理解
_NEEDS_LLM_ACTIONS = {"set_effect"}


def _color_needs_llm(actions: list[dict]) -> bool:
    """Check if any set_color/set_color_temp value is unresolvable locally."""
    from core.command_parser import COLOR_XY_MAP, COLOR_TEMP_MAP
    for a in actions:
        act = a.get("action", "")
        val = str(a.get("value", "")).strip().lower()
        if act == "set_color":
            if val in COLOR_XY_MAP:
                continue
            hex_str = val.lstrip("#")
            if len(hex_str) == 6:
                try:
                    int(hex_str, 16)
                    continue
                except ValueError:
                    pass
            return True
        if act == "set_color_temp":
            if val in COLOR_TEMP_MAP:
                continue
            return True
    return False

# APScheduler 序列化 job 时需要模块级引用，不能用实例方法
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

        # ── 事件总线 ── 组件间解耦通信，比如健康状态变化、state 切换
        self.event_bus = EventBus()

        # ── 健康监控（可选）── 追踪各 API/模型的可用性，降级时语音通知
        self.health_tracker = None
        if config.get("health", {}).get("enabled", True):
            try:
                from core.health import ComponentTracker
                self.health_tracker = ComponentTracker(config, event_bus=self.event_bus)
            except Exception as exc:
                self.logger.warning("Health tracker unavailable: %s", exc)

        # ── 音频 + 身份认证 ── 录音、ASR、声纹编码/验证、用户库、权限
        self.user_store = UserStore(config)
        self.audio_recorder = AudioRecorder(config)
        self.speaker_encoder = SpeakerEncoder(config)
        self.speaker_verifier = SpeakerVerifier(config, self.speaker_encoder, self.user_store)
        self.speech_recognizer = SpeechRecognizer(config)
        self.device_manager = DeviceManager(config, event_bus=self.event_bus)
        self.permission_manager = PermissionManager()

        # ── LLM + 对话 ── 云端大模型、对话历史持久化、用户偏好
        self.llm = LLMClient(config, tracker=self.health_tracker)
        self.conversation_store = ConversationStore(config)
        self.preference_store = UserPreferenceStore(config)

        # ── 长期记忆 ── SQLite + 向量嵌入，支持记忆存储/检索/直答
        self.memory_manager = MemoryManager(config)

        from memory.behavior_log import BehaviorLog
        mem_db = config.get("memory", {}).get("db_path", "data/memory/jarvis_memory.db")
        # BehaviorLog 和 MemoryManager 共用同一个 SQLite（不同表），WAL 模式支持并发读写
        self.behavior_log = BehaviorLog(mem_db)

        from memory.direct_answer import DirectAnswerer
        self.direct_answerer = DirectAnswerer(
            self.memory_manager.store, self.memory_manager.embedder,
        )

        # 记录最近一次交互的用户，farewell 时用来触发记忆保存
        self._last_user_id: str | None = None
        self._last_session_id: str | None = None

        # ── 定时任务（可选）── 早报、记忆维护等 cron 任务
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

        # ── 自动化引擎 ── 场景执行（如"回家模式"触发一系列设备动作）
        self.automation_engine = AutomationEngine(
            device_manager=self.device_manager,
            event_bus=self.event_bus,
            tts_callback=self.speak,
        )
        for scene_name, steps in config.get("automations", {}).items():
            if isinstance(steps, list):
                self.automation_engine.register_scene(scene_name, steps)

        # ── OLED 显示屏（可选）── RPi 上的小屏幕，显示状态/正在说的话
        self.oled = None
        if config.get("oled", {}).get("enabled", False):
            try:
                from ui.oled_display import OledDisplay
                self.oled = OledDisplay(config, self.event_bus)
                self.oled.start()
            except Exception as exc:
                self.logger.warning("OLED display unavailable: %s", exc)

        # ── 技能注册中心 ── 所有 skill（天气、灯控、提醒等）在这里统一管理
        self.skill_registry = SkillRegistry()
        self._register_skills(config)

        # ── 意图路由 + 本地执行器 + 自动化规则 ──
        self.intent_router = None
        self.local_executor = None
        self.rule_manager = None
        try:
            from core.intent_router import IntentRouter
            from core.local_executor import LocalExecutor
            from core.automation_rules import AutomationRuleManager

            self.intent_router = IntentRouter(config, tracker=self.health_tracker)

            # 自动化规则管理器：支持关键词触发（如"晚安"→关灯）和定时触发
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

        # ── 学习系统 ── 运行时教 Jarvis 新技能（调 Claude Code 生成 Python 代码）
        from core.learning_router import LearningRouter
        from core.skill_factory import SkillFactory
        self.learning_router = LearningRouter(
            skill_names=list(self.skill_registry.skill_names),
        )
        self.skill_factory = SkillFactory(
            learned_dir="skills/learned",
            project_root=str(self.config_path.parent) if self.config_path else ".",
        )

        # --- Interrupt monitor (full-duplex) ---
        from core.interrupt_monitor import InterruptMonitor
        self.interrupt_monitor = InterruptMonitor(
            config=config,
            on_interrupt=self._on_voice_interrupt,
            on_resume=self._on_voice_resume,
        )

        # ── TTS 语音合成（懒加载）── 首次调用时才初始化，避免拖慢启动
        self._tts: Any = None

        # ── 会话状态 ──
        self._cancel = threading.Event()  # 用户按 Enter 打断时设置此信号
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
        self._active_pipeline: Any = None  # current TTSPipeline for interrupt abort
        self._interrupted_response: list[str] | None = None
        self._pipeline_lock = threading.Lock()

        # 预热 embedding 模型（后台加载，不阻塞启动）
        self._executor.submit(self.memory_manager.embedder.encode, "warmup")

        # 预热 HTTP 连接（建立 keep-alive，首次真实调用省 ~100ms TCP+TLS）
        self._executor.submit(self._prewarm_connections)

        # 预热 TTS 缓存（常用短句提前合成，首次播报零延迟）
        _PRECACHE_PHRASES = ["好的", "嗯，让我想想", "好的，灯开了", "好的，灯关了", "再见", "在的"]
        self._executor.submit(lambda: self._get_tts() and self._get_tts().precache(_PRECACHE_PHRASES))

        # 预热 ASR 模型（首次加载 SenseVoice 需要 ~2s）
        self._executor.submit(self.speech_recognizer.transcribe, np.zeros(16000, dtype=np.float32))

        # ── 健康监控启动 ── 状态变化时语音通知 + 定期主动探测 API 可用性
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

        # API 可达性探测：只请求 /models 端点，不消耗 token，免费
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

        # 本地模型文件探测：检查 ASR 模型文件是否存在
        asr_model = Path(config.get("asr", {}).get(
            "sensevoice_model", "data/sensevoice-small-int8",
        ))
        if asr_model.exists():
            model_file = asr_model / "model.int8.onnx"
            tracker.register_probe("asr.sensevoice", lambda: model_file.exists())

        # 定期执行所有探测（默认 60s 一次）
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

                    if response == "farewell":
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
                        self.speak("在的。")
                        listening_for_wake = False
                        self._last_interaction = time.monotonic()
                else:
                    # 对话模式：唤醒词触发后进入，超时无交互自动回到监听
                    elapsed = time.monotonic() - self._last_interaction
                    if elapsed > self.silence_timeout:
                        self.logger.info("Session timeout after %.0fs silence.", elapsed)
                        listening_for_wake = True
                        continue

                    try:
                        # 暂停唤醒词音频流，改用 AudioRecorder 录音（避免两个流抢麦克风）
                        stream.stop()
                        audio = self.audio_recorder.record(self.utterance_duration)

                        response = self.handle_utterance(audio)
                        # 等 TTS 播完再恢复麦克风，否则自己的声音会被录进去
                        self._wait_tts()

                        if response == "farewell":
                            time.sleep(2.5)
                            detector.reset()
                            stream.start()
                            # 清空麦克风缓冲区，防止 TTS 残留音频误触发唤醒词
                            try:
                                while stream.read_available > 0:
                                    stream.read(stream.read_available)
                            except Exception:
                                pass
                            listening_for_wake = True
                        else:
                            stream.start()
                            self._last_interaction = time.monotonic()
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
        _t0 = time.monotonic()

        # ① 通知 UI 进入"聆听"状态
        self.event_bus.emit("jarvis.state_changed", {"state": "listening"})

        # ② 并行：声纹验证 + 语音识别（两个都是 CPU 密集，互不依赖）
        self._wait_tts()  # 先确保上一轮 TTS 播完，否则会把自己的声音录进去
        verify_future = self._executor.submit(self.speaker_verifier.verify, np.copy(audio))
        asr_future = self._executor.submit(self.speech_recognizer.transcribe, np.copy(audio))
        verification = verify_future.result()
        transcription = asr_future.result()
        _t_asr = time.monotonic()

        text = transcription.text.strip()
        detected_emotion = getattr(transcription, "emotion", "") or ""
        if not text:
            self.event_bus.emit("jarvis.state_changed", {"state": "idle"})
            self.logger.info("Empty ASR result, staying silent")
            return ""

        print(f"⏱ ASR+声纹: {(_t_asr - _t0)*1000:.0f}ms")

        # ③ 解析用户身份：声纹匹配 → user_id → 查角色和显示名
        user_id = verification.user if verification.verified else None
        # 没有注册用户时默认当 owner（单用户开发模式）
        if user_id is None and not self.user_store.get_all_users():
            user_id = "default_user"
        user_name = self._resolve_display_name(user_id) or "用户"
        user_role = self._resolve_role(user_id) if user_id != "default_user" else "owner"
        confidence = verification.confidence

        # ④ 打印识别结果（终端调试用）
        if user_id:
            self.logger.info(
                "Identified: %s (%.2f) said: %s", user_name, confidence, text,
            )
            print(f"🎤 {user_name} ({confidence:.2f}): {text}")
        else:
            self.logger.info("Unidentified speaker (%.2f) said: %s", confidence, text)
            print(f"🎤 Guest ({confidence:.2f}): {text}")

        session_id = user_id or "_guest"
        self._last_user_id = user_id
        self._last_session_id = session_id

        # ⑤ 进入共享处理流水线（语音/文本两条路径共用 _process_turn）
        def _voice_output(sentence: str) -> None:
            self.event_bus.emit("jarvis.state_changed", {"state": "speaking"})
            print(f"🤖 Jarvis: {sentence}")
            self._speak_nonblocking(sentence, emotion=detected_emotion)

        response_text = self._process_turn(
            text,
            emotion=detected_emotion,
            session_id=session_id,
            user_id=user_id or "default_user",
            user_name=user_name,
            user_role=user_role,
            output_fn=_voice_output,
            create_tts_pipeline=self._create_tts_pipeline,
        )

        _t_end = time.monotonic()
        print(f"⏱ 总耗时: {(_t_end - _t0)*1000:.0f}ms")

        return response_text

    # ══════════════════════════════════════════════════════════════
    # 核心处理流水线（语音和文本两条入口共用）
    # ══════════════════════════════════════════════════════════════

    def _process_turn(  # noqa: C901 — intentionally long; single pipeline
        self,
        text: str,
        *,
        emotion: str = "",
        session_id: str,
        user_id: str = "default_user",
        user_name: str = "用户",
        user_role: str = "owner",
        output_fn: Callable[[str], None],
        create_tts_pipeline: Callable[[], Any] | None = None,
    ) -> str:
        """Process a text turn through the full pipeline.

        Handles: farewell → escalation → memory shortcut → learning →
        keyword trigger → memory query → direct answer → intent route →
        route dispatch → cloud LLM → save → behavior log.

        Args:
            text: User message.
            emotion: Detected emotion label.
            session_id: Conversation session identifier.
            user_id: Resolved user identifier.
            user_name: Display name for LLM context.
            user_role: Permission role (owner/guest/etc).
            output_fn: Called with each output sentence (TTS or callback).
            create_tts_pipeline: Factory for TTS streaming pipeline (voice
                path only).  When *None*, streaming sentences are delivered
                via *output_fn* directly.

        Returns:
            Full assistant response text.
        """
        # 加载对话历史（用于多轮上下文）
        history = self.conversation_store.get_history(session_id)

        # Resume from interruption: "继续说" etc
        from core.interrupt_monitor import RESUME_KEYWORDS
        _resume_sentences: list[str] | None = None
        with self._pipeline_lock:
            if self._interrupted_response and any(kw in text for kw in RESUME_KEYWORDS):
                _resume_sentences = self._interrupted_response
                self._interrupted_response = None
        if _resume_sentences is not None:
            for s in _resume_sentences:
                output_fn(s)
            full_text = "".join(_resume_sentences)
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": full_text})
            self.conversation_store.replace(session_id, history)
            return full_text

        # ── 快捷路径 1：告别 ── 直接本地回复，不走任何 API，~120ms
        if self._is_farewell(text):
            reply = "再见。"
            self.logger.info("Farewell shortcut: %s", text[:60])
            output_fn(reply)
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": reply})
            self.conversation_store.replace(session_id, history)
            if user_id:
                self._executor.submit(
                    self.memory_manager.save, history, user_id, session_id,
                    emotion,
                )
            return "farewell"

        # ── 升级模式 ── 用户说"仔细想想"等前缀词，本轮临时切换到更强的 LLM
        _escalated = False
        _original_preset: str | None = None
        for _esc_kw in _ESCALATION_KEYWORDS:
            if text.startswith(_esc_kw):
                _original_preset = self.llm.active_preset
                text = text[len(_esc_kw):].lstrip(" ，,、:：")
                try:
                    self.llm.switch_model("deep")
                    _escalated = True
                    self.logger.info("Escalated to deep preset for this turn")
                    if create_tts_pipeline:
                        output_fn("嗯，让我想想")
                except (ValueError, Exception) as exc:
                    self.logger.warning("Escalation switch failed: %s", exc)
                break

        # ── 快捷路径 2：记忆存储 ── "记住/记下/别忘了" → 直接确认，后台异步提取
        if any(text.startswith(kw) or kw in text[:10] for kw in _REMEMBER_KEYWORDS):
            if "每次" not in text:
                reply = "好的，记住了。"
                self.logger.info("Memory shortcut: %s", text[:60])
                output_fn(reply)
                history.append({"role": "user", "content": text})
                history.append({"role": "assistant", "content": reply})
                self.conversation_store.replace(session_id, history)
                if user_id:
                    self._executor.submit(
                        self.memory_manager.save, history, user_id, session_id,
                        emotion,
                    )
                return reply

        # ── 快捷路径 3：学习意图 ── "学会查XX" → 后台调 Claude Code 生成新技能
        if hasattr(self, "learning_router"):
            learning = self.learning_router.detect(text)
            if learning and learning.mode == "create":
                self.logger.info("Learning intent: create — %s", learning.description[:60])
                learn_response = self._learn_create(learning, user_id)
                output_fn(learn_response)
                history.append({"role": "user", "content": text})
                history.append({"role": "assistant", "content": learn_response})
                self.conversation_store.replace(session_id, history)
                if user_id:
                    self.behavior_log.log(user_id, "conversation", {
                        "text": text[:100], "route": "learn_create",
                    })
                return learn_response
            # config/compose 模式交给后面的云端 LLM 处理

        # ── 关键词规则匹配 ── 用户定义的触发词（如"晚安"→关灯+关窗帘）
        _t_think = time.monotonic()
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

        # ── 记忆检索 ── 向量搜索相关记忆，作为 context 传给后续 LLM（~50-100ms）
        memory_context = ""
        if user_id:
            try:
                memory_context = self.memory_manager.query(text, user_id)
            except Exception as exc:
                self.logger.warning("Memory query failed: %s", exc)

        # ── 记忆直答（L1）── 如果记忆里有确切答案就直接回复，完全跳过 LLM
        if user_id and response_text is None:
            try:
                direct = self.direct_answerer.try_answer(text, user_id)
                if direct:
                    _t_da = time.monotonic()
                    print(f"⏱ DA直答: {(_t_da - _t_think)*1000:.0f}ms")
                    self.logger.info("Level 1 direct answer: %s", direct[:60])
                    output_fn(direct)
                    self.behavior_log.log(user_id, "conversation", {
                        "text": text[:100],
                        "route": "memory_l1",
                        "answer": direct[:100],
                    })
                    if _escalated and _original_preset is not None:
                        try:
                            self.llm.switch_model(_original_preset)
                        except Exception:
                            pass
                    return direct
            except Exception as exc:
                self.logger.warning("Level 1 answer failed: %s", exc)

        # ── 意图路由 ── 单次 Groq 调用，同时完成意图分类 + 简单回答
        route = None
        if response_text is None and self.intent_router and self.local_executor:
            try:
                route = self.intent_router.route_and_respond(
                    text,
                    conversation_history=history,
                    memory_context=memory_context,
                    user_emotion=emotion,
                )
            except Exception as exc:
                self.logger.warning("Unified route failed: %s", exc)

        _t_route = time.monotonic()
        if route:
            print(f"⏱ 意图路由: {(_t_route - _t_think)*1000:.0f}ms → {route.tier}/{route.intent} ({route.provider})")
        else:
            print(f"⏱ 意图路由: {(_t_route - _t_think)*1000:.0f}ms → 无路由")

        if response_text is None and route is not None:
            if route.text_response:
                response_text = route.text_response
            elif route.tier == "local":
                if route.intent == "smart_home":
                    needs_llm = (
                        any(a.get("action") in _NEEDS_LLM_ACTIONS for a in route.actions)
                        or _color_needs_llm(route.actions)
                    )
                    if needs_llm:
                        device_ids = {a.get("device_id") for a in route.actions if a.get("device_id")}
                        status_parts = []
                        for did in device_ids:
                            try:
                                status = self.device_manager.get_device(did).get_status()
                                status_parts.append(f"{did}: {status}")
                            except Exception:
                                pass
                        if status_parts:
                            memory_context += f"\n[当前设备状态] {'; '.join(status_parts)}"
                    else:
                        ar = self.local_executor.execute_smart_home(
                            route.actions, user_role, response=route.response,
                        )
                elif route.intent == "info_query":
                    if route.sub_type in ("news", "stocks", "weather"):
                        ar = self.local_executor.execute_info_query(
                            route.sub_type, route.query, user_role,
                        )
                    else:
                        if user_id:
                            try:
                                mem_answer = self.direct_answerer.try_answer(text, user_id)
                                if mem_answer:
                                    ar = ActionResponse(Action.RESPONSE, mem_answer)
                            except Exception:
                                pass
                        if ar is None:
                            ar = ActionResponse(
                                Action.RESPONSE,
                                "这个我暂时没有对应的技能。你可以说'小月，学会查这个'来教我。",
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
                    else:
                        response_text = ar.text

        # 本地执行计时
        if response_text is not None:
            _t_local = time.monotonic()
            print(f"⏱ 本地执行: {(_t_local - _t_route)*1000:.0f}ms")

        # ── 云端 LLM ── 前面都没处理掉才到这里。两种模式：
        #   - REQLLM 转述：本地查到了数据，让 LLM 用小月语气润色
        #   - 完整 cloud：直接让 LLM 回答（支持 streaming + tool-use 循环）
        _t_llm_start = time.monotonic()
        if response_text is None and not self._cancel.is_set():
            tools = self.skill_registry.get_tool_definitions(user_role)
            tts_pipeline = create_tts_pipeline() if create_tts_pipeline else None
            with self._pipeline_lock:
                self._active_pipeline = tts_pipeline

            # Start interrupt monitoring during TTS playback
            self.interrupt_monitor.start()

            _t_first_sentence = [None]  # mutable for closure

            def _on_sentence(sentence: str) -> None:
                if self._cancel.is_set():
                    return
                nonlocal sentence_count
                sentence_count += 1
                if sentence_count == 1:
                    _t_first_sentence[0] = time.monotonic()
                    print(f"⏱ LLM首句: {(_t_first_sentence[0] - _t_llm_start)*1000:.0f}ms")
                print(f"🤖 Jarvis: {sentence}")
                if tts_pipeline:
                    st = SentenceType.FIRST if sentence_count == 1 else SentenceType.MIDDLE
                    tts_pipeline.submit(sentence, st, emotion=emotion)
                else:
                    self._wait_tts()
                    output_fn(sentence)

            self.event_bus.emit("jarvis.state_changed", {"state": "speaking"})

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
            finally:
                # Stop interrupt monitor, get accumulated audio
                interrupt_audio = self.interrupt_monitor.stop()
                if tts_pipeline:
                    if sentence_count > 0:
                        tts_pipeline.finish()
                        tts_pipeline.wait_done()
                    tts_pipeline.stop()
                with self._pipeline_lock:
                    self._active_pipeline = None

        # ── 持久化 ── 保存对话历史 + 后台异步提取记忆（GPT-4o-mini）
        cloud_path = updated_messages is not None
        if cloud_path:
            self.conversation_store.replace(session_id, updated_messages)
            if user_id:
                self._executor.submit(
                    self.memory_manager.save, updated_messages, user_id, session_id,
                    emotion,
                )
        elif response_text:
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": response_text})
            self.conversation_store.replace(session_id, history)
            updated_messages = history

        # ── 行为日志 ── 记录技能调用、情绪、路由路径，用于后续分析
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
                "emotion": emotion,
                "route": "local" if response_text and not cloud_path else "cloud",
            })

        # ── 非流式输出 ── 本地路径的响应在这里一次性播报（流式的已经在上面逐句播了）
        if sentence_count == 0 and not self._cancel.is_set() and response_text:
            if self.oled:
                self.oled.set_speaking_text(response_text)
            output_fn(response_text)

        # 如果本轮升级了模型，恢复原来的 preset
        if _escalated and _original_preset is not None:
            try:
                self.llm.switch_model(_original_preset)
                self.logger.info("Restored preset to '%s' after escalation", _original_preset)
            except Exception as exc:
                self.logger.warning("Failed to restore preset: %s", exc)

        return response_text

    # ══════════════════════════════════════════════════════════════
    # 纯文本入口（Web 前端用，不走录音/TTS）
    # ══════════════════════════════════════════════════════════════

    def handle_text(
        self,
        text: str,
        session_id: str = "_web",
        on_sentence: Any = None,
        emotion: str = "",
    ) -> str:
        """Process a text message without audio/TTS.

        Thin wrapper around :meth:`_process_turn` for the web frontend.
        """
        def _text_output(sentence: str) -> None:
            if on_sentence:
                on_sentence(sentence, emotion=emotion)

        return self._process_turn(
            text,
            emotion=emotion,
            session_id=session_id,
            output_fn=_text_output,
        )


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

    def _on_voice_interrupt(self) -> None:
        """Called by InterruptMonitor when an interrupt keyword is detected."""
        self.logger.info("Voice interrupt detected")
        self._cancel.set()
        self._cancel_current()

    def _on_voice_resume(self) -> None:
        """Called by InterruptMonitor when a resume keyword is detected."""
        self.logger.info("Voice resume detected")
        self._cancel.set()
        self._cancel_current()

    def _cancel_current(self) -> None:
        """Cancel current TTS and reset state after user interrupt."""
        with self._pipeline_lock:
            self._interrupted_response = None  # clear stale buffer first
            if self._active_pipeline:
                remaining = self._active_pipeline.abort()
                if remaining:
                    self._interrupted_response = remaining
        # Kill non-pipeline TTS (local shortcuts)
        if self._tts_future and not self._tts_future.done():
            self._tts_future.cancel()
        tts = self._get_tts()
        if tts:
            tts.stop()
        # Cancel learning subprocess
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

        from skills.model_switch import ModelSwitchSkill
        self.skill_registry.register(ModelSwitchSkill(self.llm))

        # 实时数据技能（可选）：新闻、股票等需要联网查询的数据
        if config.get("skills", {}).get("realtime_data", {}).get("enabled", False):
            try:
                from skills.realtime_data import RealTimeDataSkill
                rt_skill = RealTimeDataSkill(config)
                self.skill_registry.register(rt_skill)
                if self.scheduler and self.scheduler.available:
                    rt_skill.set_scheduler(self.scheduler)
            except Exception as exc:
                self.logger.warning("RealTimeData skill unavailable: %s", exc)

        # 定时任务技能（可选）：让 LLM 能创建/管理 cron 任务
        if self.scheduler and self.scheduler.available:
            try:
                from skills.scheduler_skill import SchedulerSkill
                self.skill_registry.register(SchedulerSkill(config, self.scheduler))
            except Exception as exc:
                self.logger.warning("Scheduler skill unavailable: %s", exc)

        # 远程控制技能（可选）：通过网络控制其他设备
        if config.get("remote", {}).get("enabled", False):
            try:
                from skills.remote_control import RemoteControlSkill
                self.skill_registry.register(RemoteControlSkill(config))
            except Exception as exc:
                self.logger.warning("Remote control skill unavailable: %s", exc)

        # 健康状态技能（可选）：让用户能语音询问"系统状态怎么样"
        if self.health_tracker:
            try:
                from skills.health_skill import HealthSkill
                self.skill_registry.register(HealthSkill(self.health_tracker))
            except Exception as exc:
                self.logger.warning("Health skill unavailable: %s", exc)

        # ── 加载用户教会的技能 ── 从 skills/learned/ 目录动态扫描
        from core.skill_loader import SkillLoader
        self.skill_loader = SkillLoader("skills/learned")
        for skill in self.skill_loader.scan():
            try:
                self.skill_registry.register(skill)
            except Exception as exc:
                self.logger.warning("Failed to register learned skill %s: %s", skill.skill_name, exc)

        # ── 技能管理 ── 让 LLM 能列出/删除/禁用已有技能
        from skills.skill_mgmt import SkillManagementSkill
        self.skill_registry.register(SkillManagementSkill(self.skill_loader, self.skill_registry))

        # 把 TTS 回调注入 TimeSkill，这样计时器到期时能语音播报
        time_skill = self.skill_registry._skills.get("time")
        if time_skill and hasattr(time_skill, "set_timer_callback"):
            time_skill.set_timer_callback(self.speak)

        self.logger.info(
            "Registered %d skills: %s",
            len(self.skill_registry.skill_names),
            ", ".join(self.skill_registry.skill_names),
        )

    def _learn_create(self, intent: Any, user_id: str | None) -> str:
        """创造型：后台调用 Claude Code 技能工厂，不阻塞对话。"""
        skill_id = self.skill_factory._slugify(intent.description)
        if self.skill_factory.has_skill(skill_id):
            return f"我已经会{intent.description}了。要重新学一个不同版本吗？说'重新学{intent.description}'。"
        self._executor.submit(self._learn_create_bg, intent, user_id)
        return "好的，我在后台学，你继续说。"

    def _learn_create_bg(self, intent: Any, user_id: str | None) -> None:
        """后台技能学习 — 完成后语音通知。"""
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
                            "status": "pending_review",
                        })
                self.learning_router.update_skills(list(self.skill_registry.skill_names))
            except Exception as exc:
                self.logger.warning("Failed to hot-load new skill: %s", exc)
                self.speak(f"技能文件生成了但加载失败：{exc}")
                return

            if user_id:
                self.behavior_log.log(user_id, "skill_learned", {
                    "skill": result["skill_name"],
                    "description": intent.description,
                })
            self.speak(f"学会了！现在可以{intent.description}了。")
        else:
            self.speak(f"没学会，{result['message']}")

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
        import os as _os
        _os.system("clear" if _os.name != "nt" else "cls")
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
