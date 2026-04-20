"""Jarvis AI Voice Assistant — main entry point.

Supports two modes:
  - ``python jarvis.py``            → always-listening with wake word
  - ``python jarvis.py --no-wake``  → press-Enter-to-talk (no Porcupine needed)
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import threading
import time
import traceback
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
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
from core.tool_registry import ToolRegistry

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
        from core.asr_normalizer import ASRNormalizer
        self.asr_normalizer = ASRNormalizer(config)
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

        from memory.trace import TraceLog
        self.trace_log = TraceLog(mem_db)
        self._turn_counter: dict[str, int] = {}  # session_id → turn count

        # ── Trace v3 launch session ──
        # The conversation_store keys history by user_id (history persists
        # across launches). For trace analytics we want a per-launch session
        # boundary so (session_id, turn_id) is meaningful. Format
        # YYYYMMDDTHHMMSS-<8hex> sorts chronologically and stays unique.
        self._app_session_id = (
            f"{datetime.now():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
        )

        # ── Trace v3 stable identifiers (computed once) ──
        # prompt_version: 16-char SHA prefix of personality.py contents.
        # Changes whenever the system prompt definition changes; lets us
        # filter traces by prompt-iteration when comparing behavior.
        try:
            personality_src = Path(__file__).parent / "core" / "personality.py"
            self._prompt_version: str | None = hashlib.sha256(
                personality_src.read_bytes(),
            ).hexdigest()[:16]
        except OSError as exc:
            self.logger.warning("Could not hash personality.py for prompt_version: %s", exc)
            self._prompt_version = None

        # ── Trace v3 carry-across-turn state ──
        # _last_trace_id: the trace.id of the previous turn — used by
        #   detect_outcome on the current turn to schedule an outcome
        #   update on the PREVIOUS turn (the one we just judged).
        # _pending_outcome_update: (prev_trace_id, signal) staged in
        #   _process_turn entry, applied after the current turn flushes.
        self._last_trace_id: int | None = None
        self._pending_outcome_update: tuple[int, int] | None = None

        # ── Trace v3 voice-path captures (set per turn via _process_turn kwargs) ──
        # Reset every turn in _reset_turn_state. Stay None for text-only path.
        self._last_asr_confidence: float | None = None
        self._last_vad_duration_ms: int | None = None
        self._last_asr_ms: int | None = None
        # Time (perf_counter) at which AudioStreamPlayer first emitted real
        # samples for the current turn. Set by the on_first_chunk callback
        # (audio thread). Single attr-assign is GIL-atomic, no lock needed.
        self._first_audio_at: float | None = None

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

        # ── 工具注册中心 (v2) ── @jarvis_tool 函数 + YAML skills 统一管理
        import tools.smart_home
        tools.smart_home.init(self.device_manager, self.permission_manager)
        import tools.time_utils
        tools.time_utils.init(tts_callback=self.speak)
        import tools.reminders
        tools.reminders.init(
            filepath=config.get("skills", {}).get("reminders", {}).get("path", "data/reminders.json"),
            scheduler=self.scheduler,
            tts_callback=self.speak,
            event_bus=self.event_bus,
        )
        import tools.todos
        tools.todos.init(
            persist_dir=config.get("skills", {}).get("todos", {}).get("dir", "data/todos"),
        )

        self.tool_registry = ToolRegistry(config)

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

            self.local_executor = LocalExecutor(self.tool_registry, self.rule_manager)
        except Exception as exc:
            self.logger.warning("Intent router unavailable: %s", exc)

        # --- Interrupt monitor (full-duplex) ---
        # Share main-path SpeechRecognizer + ASRNormalizer so the interrupt
        # path uses the same model instance (no double load) and the same
        # three-layer normalization (B4 decision applied consistently).
        from core.interrupt_monitor import InterruptMonitor
        self.interrupt_monitor = InterruptMonitor(
            config=config,
            on_interrupt=self._on_voice_interrupt,
            on_resume=self._on_voice_resume,
            on_soft_pause=self._on_soft_pause,
            on_soft_resume=self._on_soft_resume,
            speech_recognizer=self.speech_recognizer,
            asr_normalizer=self.asr_normalizer,
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
        # WP5: snapshot of sentences fully played before the most recent interrupt.
        # Set in _cancel_current; consumed (and cleared) in _process_turn before
        # writing history. None means no pending interrupt to inject.
        self._interrupt_played_texts: list[str] | None = None

        # 预热 embedding 模型（后台加载，不阻塞启动）
        self._executor.submit(self.memory_manager.embedder.encode, "warmup")

        # 预热 HTTP 连接（建立 keep-alive，首次真实调用省 ~100ms TCP+TLS）
        self._executor.submit(self._prewarm_connections)

        # 预热 TTS 缓存（常用短句提前合成，首次播报零延迟）
        _PRECACHE_PHRASES = ["好的", "嗯，让我想想", "好的，灯开了", "好的，灯关了", "再见", "在的"]
        self._executor.submit(lambda: self._get_tts() and self._get_tts().precache(_PRECACHE_PHRASES))

        # 预热 ASR 模型（首次加载 SenseVoice 需要 ~2s）
        self._executor.submit(self.speech_recognizer.transcribe, np.zeros(16000, dtype=np.float32))

        # 预热 Silero VAD（录音 + 打断各一个实例）
        if getattr(self.audio_recorder, "_vad", None) is not None:
            self._executor.submit(
                self.audio_recorder._vad.accept_waveform,
                np.zeros(512, dtype=np.float32),
            )
        if hasattr(self, "interrupt_monitor"):
            self._executor.submit(self._warmup_interrupt_vad)

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
        # Trace v3: snapshot ASR + VAD metadata for the upcoming turn.
        # Passed as explicit kwargs to _process_turn so reset semantics are
        # localized (no cross-method instance-attr races).
        _asr_ms = int((_t_asr - _t0) * 1000)
        _asr_confidence = getattr(transcription, "confidence", None)
        try:
            _vad = getattr(self.audio_recorder, "_vad", None)
            _vad_duration_ms = _vad.active_duration_ms if _vad is not None else None
        except AttributeError:
            _vad_duration_ms = None
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
            trigger_source="wake_word",
            asr_ms=_asr_ms,
            asr_confidence=_asr_confidence,
            vad_duration_ms=_vad_duration_ms,
        )

        _t_end = time.monotonic()
        print(f"⏱ 总耗时: {(_t_end - _t0)*1000:.0f}ms")

        return response_text

    # ══════════════════════════════════════════════════════════════
    # 核心处理流水线（语音和文本两条入口共用）
    # ══════════════════════════════════════════════════════════════

    def _process_turn(
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
        trigger_source: str = "wake_word",
        asr_ms: int | None = None,
        asr_confidence: float | None = None,
        vad_duration_ms: int | None = None,
    ) -> str:
        """Wrap _process_turn_inner with try/finally to flush trace v3.

        Catches exceptions to record `end_reason="error"` + traceback, then
        re-raises so callers see the original failure. The finally block
        always flushes a trace row, even on early returns from the inner.

        Args:
            text: Raw user message (pre-normalize).
            trigger_source: One of "wake_word", "continuation", "web_text",
                "web_voice", "proactive", "test". Plumbed from entry point.
            asr_ms: Voice-path only — ms spent in ASR + speaker verify.
            asr_confidence: Voice-path only — SenseVoice confidence [0, 1].
            vad_duration_ms: Voice-path only — total active-speech duration.
            (other args forwarded unchanged to _process_turn_inner)

        Returns:
            Full assistant response text (or "farewell" signal).
        """
        turn_start = time.monotonic()
        self._reset_turn_state(text, trigger_source=trigger_source)
        # Stash voice-path snapshots after the reset (these would otherwise
        # be clobbered by reset_turn_state's defaults).
        self._last_asr_ms = asr_ms
        self._last_asr_confidence = asr_confidence
        self._last_vad_duration_ms = vad_duration_ms
        # Trace v3: arm the AudioStreamPlayer first-chunk callback BEFORE
        # the inner runs. This way ttfs_ms is captured for ANY path that
        # plays TTS audio (cloud, farewell shortcut, memory_l1, etc.) —
        # not just the cloud path.
        self._arm_tts_first_chunk()
        try:
            result = self._process_turn_inner(
                text,
                emotion=emotion,
                session_id=session_id,
                user_id=user_id,
                user_name=user_name,
                user_role=user_role,
                output_fn=output_fn,
                create_tts_pipeline=create_tts_pipeline,
            )
            if not self._last_end_reason:
                self._last_end_reason = (
                    "interrupted" if self._cancel.is_set() else "success"
                )
            return result
        except Exception as exc:
            self._last_end_reason = "error"
            self._last_error = (
                f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[:2000]}"
            )
            raise
        finally:
            if user_id:
                self._flush_trace(
                    text=text,
                    session_id=session_id,
                    user_id=user_id,
                    emotion=emotion,
                    turn_start=turn_start,
                )

    def _process_turn_inner(  # noqa: C901 — intentionally long; single pipeline
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
        # P0-B: clear stale interrupt-played state from any prior turn.
        # Non-cloud_path returns (resume / farewell / direct_answer / etc.)
        # don't consume _interrupt_played_texts, so without this reset a
        # later cloud_path turn would mis-truncate its assistant response
        # against the previous interrupt's played sentences.
        # _cancel_current re-populates this attribute mid-turn if the user
        # interrupts during streaming, and the consumer below clears it again.
        self._interrupt_played_texts = None

        # T1.5: normalize on shared pipeline so text-path (handle_text/web/MQTT)
        # also benefits. Corrections have require_context guards → safe for
        # non-voice input.
        _raw_text = text
        text = self.asr_normalizer.normalize(text)
        if text != _raw_text:
            self.logger.info("ASR normalized: %r -> %r", _raw_text, text)

        # 加载对话历史（用于多轮上下文）
        history = self.conversation_store.get_history(session_id)
        self._last_history_turns = len(history)
        # Trace v3: snapshot pre-turn history length so _flush_trace can
        # extract tool_use blocks ONLY from messages added this turn (not
        # leak few-shot examples and stale tool calls from history).
        self._last_history_len_before = len(history)
        # Trace v3: tts_emotion describes audio actually played. Voice-path
        # turns (create_tts_pipeline factory present) record the emotion;
        # text-path turns leave it None even if user_emotion is set.
        if create_tts_pipeline is not None:
            self._last_tts_emotion = emotion or None
        # All other _last_* trace attributes were reset in the outer
        # _process_turn wrapper via _reset_turn_state(). The inner only
        # *populates* them at hook points; never re-resets here.

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
            self._last_path = "resume"
            print(f"path={self._last_path}")
            self._last_response_text = full_text
            return full_text

        # ── 快捷路径 1：告别 ── 直接本地回复，不走任何 API，~120ms
        if self._is_farewell(text):
            reply = "再见。"
            self.logger.info("Farewell shortcut: %s", text[:60])
            self._last_path = "farewell"
            self._last_farewell_match = text.strip().lower()
            print(f"path={self._last_path}")
            output_fn(reply)
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": reply})
            self.conversation_store.replace(session_id, history)
            if user_id:
                self._executor.submit(
                    self.memory_manager.save, history, user_id, session_id,
                    emotion,
                )
            self._last_response_text = reply
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
                    self._last_escalation = {
                        "keyword": _esc_kw,
                        "from": _original_preset,
                        "to": "deep",
                    }
                    self.logger.info("Escalated to deep preset for this turn")
                    if create_tts_pipeline:
                        output_fn("嗯，让我想想")
                except (ValueError, Exception) as exc:
                    self.logger.warning("Escalation switch failed: %s", exc)
                break

        # ── 快捷路径 2：记忆存储 ── "记住/记下/别忘了" → 直接确认，后台异步提取
        _matched_kw = next((kw for kw in _REMEMBER_KEYWORDS if text.startswith(kw) or kw in text[:10]), None)
        if _matched_kw:
            if "每次" not in text:
                reply = "好的，记住了。"
                self.logger.info("Memory shortcut: %s", text[:60])
                self._last_path = "memory_shortcut"
                self._last_memory_keyword = _matched_kw
                print(f"path={self._last_path}")
                output_fn(reply)
                history.append({"role": "user", "content": text})
                history.append({"role": "assistant", "content": reply})
                self.conversation_store.replace(session_id, history)
                if user_id:
                    self._executor.submit(
                        self.memory_manager.save, history, user_id, session_id,
                        emotion,
                    )
                self._last_response_text = reply
                return reply

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
                self._last_keyword_rule = {"rule_name": rule_name, "actions": keyword_actions}
                if keyword_actions and keyword_actions[0].get("skill"):
                    ar = self.local_executor.execute_skill_alias(
                        keyword_actions, user_role,
                    )
                    if ar.action == Action.REQLLM:
                        use_llm_rephrase = True
                        self._last_reqllm = True
                    else:
                        response_text = ar.text
                        self._last_path = "keyword_rule"
                else:
                    ar = self.local_executor.execute_smart_home(
                        keyword_actions, user_role, response=f"好的，{rule_name}已执行。",
                    )
                    response_text = ar.text
                    self._last_path = "keyword_rule"

        # ── 记忆检索 ── 向量搜索相关记忆，作为 context 传给后续 LLM（~50-100ms）
        memory_context = ""
        if user_id:
            # Trace v3: reset retriever's last_hits so we know whether the
            # current turn's build_stable_prefix actually called retrieve.
            try:
                self.memory_manager.retriever.last_hits = []
            except AttributeError:
                pass
            _t_mem_start = time.monotonic()
            try:
                memory_context = self.memory_manager.build_stable_prefix(
                    recent_turns=history,
                    current_input=text,
                )
                _mem_ms = int((time.monotonic() - _t_mem_start) * 1000)
                self._last_memory_hits = memory_context
                self._last_timings["memory_query_ms"] = _mem_ms
                if memory_context:
                    _mem_count = memory_context.count("\n- ")
                    print(f"🧠 记忆检索: {_mem_count} 条相关记忆 ({_mem_ms}ms)")
                # Trace v3: snapshot IDs+scores. Empty arrays mean "queried,
                # no top-k retrieval ran" (e.g. <=20 memories so prefix used
                # all-active branch). Distinct from NULL = "did not query".
                hits = getattr(self.memory_manager.retriever, "last_hits", [])
                self._last_memory_query_ids = {
                    "observation_ids": [h.get("id") for h in hits if h.get("id") is not None],
                    "top_k_scores":    [round(float(h.get("_score", 0.0)), 4) for h in hits],
                }
            except Exception as exc:
                self.logger.warning("Memory query failed: %s", exc)

        # ── 记忆直答（L1）── 如果记忆里有确切答案就直接回复，完全跳过 LLM
        if user_id and response_text is None:
            try:
                direct = self.direct_answerer.try_answer(text, user_id)
                if direct:
                    _t_da = time.monotonic()
                    _da_ms = int((_t_da - _t_think)*1000)
                    print(f"⏱ DA直答: {_da_ms}ms")
                    self._last_timings["direct_answer_ms"] = _da_ms
                    self._last_direct_answer = {"answer": direct, "latency_ms": _da_ms}
                    self.logger.info("Level 1 direct answer: %s", direct[:60])
                    self._last_path = "memory_l1"
                    print(f"path={self._last_path}")
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
                    self._last_response_text = direct
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
        _route_ms = int((_t_route - _t_think)*1000)
        self._last_route = route
        self._last_timings["route_ms"] = _route_ms
        # Trace v3: router self-reported confidence (NOT a calibrated logprob).
        # See trace.py log_turn docstring for the caveat.
        if route is not None:
            self._last_intent_route_score = route.confidence
        if route:
            print(f"⏱ 路由: {_route_ms}ms → {route.tier}/{route.intent} ({route.provider}, {route.confidence:.2f})")
            if route.actions:
                for a in route.actions:
                    val_str = f" ({a['value']})" if a.get("value") else ""
                    print(f"   📋 {a.get('device_id', '?')} → {a.get('action', '?')}{val_str}")
        else:
            print(f"⏱ 路由: {_route_ms}ms → 无路由")

        if response_text is None and route is not None:
            if route.text_response:
                response_text = route.text_response
                self._last_path = "local"
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
                        self._last_device_ops = route.actions
                        for a in route.actions:
                            did = a.get("device_id")
                            if did:
                                try:
                                    st = self.device_manager.get_device(did).get_status()
                                    on_str = "ON" if st.get("is_on") else "OFF"
                                    extras = []
                                    if "brightness" in st:
                                        extras.append(f"brightness={st['brightness']}")
                                    if "color_temp" in st:
                                        extras.append(f"color_temp={st['color_temp']}")
                                    if "color" in st and st["color"] != "white":
                                        extras.append(f"color={st['color']}")
                                    if "temperature" in st:
                                        extras.append(f"temp={st['temperature']}°C")
                                    if "is_locked" in st:
                                        extras.append("locked" if st["is_locked"] else "unlocked")
                                    extra_str = f"  {' '.join(extras)}" if extras else ""
                                    print(f"   💡 {did}: {on_str}{extra_str}")
                                except Exception:
                                    pass
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
                        self._last_reqllm = True
                        response_text = None
                    else:
                        response_text = ar.text
                        self._last_path = "local"

        # 本地执行计时
        if response_text is not None:
            _t_local = time.monotonic()
            _local_ms = int((_t_local - _t_route)*1000)
            print(f"⏱ 本地执行: {_local_ms}ms")
            self._last_timings["local_exec_ms"] = _local_ms

        # ── 云端 LLM ── 前面都没处理掉才到这里。两种模式：
        #   - REQLLM 转述：本地查到了数据，让 LLM 用小月语气润色
        #   - 完整 cloud：直接让 LLM 回答（支持 streaming + tool-use 循环）
        _t_llm_start = time.monotonic()
        if response_text is None and not self._cancel.is_set():
            self._last_path = "cloud"
            # Trace v3: wrap the tool executor so each call's result + elapsed
            # ms get captured into self._last_tool_call_log. Used by
            # _flush_trace in preference to message-block scanning.
            tool_call_log: list[dict] = []
            self._last_tool_call_log = tool_call_log

            def _wrapped_tool_executor(
                name: str,
                args: dict,
                **kw: Any,
            ) -> str:
                _tool_t0 = time.monotonic()
                _tool_result: Any = ""
                try:
                    _tool_result = self.tool_registry.execute(name, args, **kw)
                    return _tool_result
                except Exception as exc:
                    _tool_result = f"<tool_error: {type(exc).__name__}: {exc}>"
                    raise
                finally:
                    tool_call_log.append({
                        "name": name,
                        "args": args,
                        "result": str(_tool_result)[:500],
                        "ms": int((time.monotonic() - _tool_t0) * 1000),
                    })

            tools = self.tool_registry.get_tool_definitions(user_role)
            tts_pipeline = create_tts_pipeline() if create_tts_pipeline else None
            with self._pipeline_lock:
                self._active_pipeline = tts_pipeline
            # Prewarm: open WS session in parallel with LLM first-token latency.
            # No-op for non-streaming engines / ws disabled. Safe if fails (logs + continues).
            if tts_pipeline is not None:
                try:
                    tts_pipeline.prewarm(emotion)
                except Exception as exc:
                    self.logger.debug("TTS prewarm skipped: %s", exc)

            # Start interrupt monitoring during TTS playback (voice path only)
            if tts_pipeline:
                self.interrupt_monitor.start()
                self.interrupt_monitor.start_mic_listener()

            _t_first_sentence = [None]  # mutable for closure

            def _on_sentence(sentence: str) -> None:
                if self._cancel.is_set():
                    return
                nonlocal sentence_count
                sentence_count += 1
                if sentence_count == 1:
                    _t_first_sentence[0] = time.monotonic()
                    _llm_first_ms = int((_t_first_sentence[0] - _t_llm_start)*1000)
                    print(f"⏱ LLM首句: {_llm_first_ms}ms")
                    self._last_timings["llm_first_ms"] = _llm_first_ms
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
                            tool_executor=_wrapped_tool_executor,
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
                if tts_pipeline:
                    self.interrupt_monitor.stop_mic_listener()
                    interrupt_audio = self.interrupt_monitor.stop()
                if tts_pipeline:
                    if sentence_count > 0:
                        tts_pipeline.finish()
                        tts_pipeline.wait_done()
                    tts_pipeline.stop()
                with self._pipeline_lock:
                    self._active_pipeline = None
                # Trace v3: harvest LLM-layer metadata from the client. Done
                # in finally so it runs even if chat_stream raised.
                try:
                    self._last_llm_metadata = self.llm.last_metadata
                    self._last_finish_reason = self.llm.last_finish_reason
                    self._last_cache_read_tokens = self.llm.last_cache_read_tokens
                except AttributeError:
                    pass  # older llm.py without v3 metadata exposure

        # ── 持久化 ── 保存对话历史 + 后台异步提取记忆（GPT-4o-mini）
        cloud_path = updated_messages is not None
        if cloud_path:
            # WP5: if an interrupt landed during this turn, rewrite the last
            # assistant message to the played-only content + append marker, so
            # the LLM's next turn knows what the user actually heard.
            if self._interrupt_played_texts is not None:
                updated_messages = self._truncate_assistant_for_interrupt(
                    updated_messages, self._interrupt_played_texts,
                )
                self._interrupt_played_texts = None
                self._last_llm_metadata["truncated_by_interrupt"] = True
                self._last_llm_metadata["full_response"] = response_text
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
        # Trace v3: stash final messages for _flush_trace to extract tool_calls.
        self._last_updated_messages = updated_messages

        # ── 行为日志 ── 记录技能调用、情绪、路由路径，用于后续分析
        # (Trace v3 log_turn happens in _flush_trace in the outer wrapper.)
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

        print(f"path={self._last_path}")
        self._last_response_text = response_text or ""
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
        *,
        user_id: str = "default_user",
        user_name: str = "用户",
        user_role: str = "owner",
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
            user_id=user_id,
            user_name=user_name,
            user_role=user_role,
            output_fn=_text_output,
            trigger_source="web_text",
        )

    # ══════════════════════════════════════════════════════════════
    # Trace v3 helpers (reset / flush / TTS first-chunk wiring)
    # ══════════════════════════════════════════════════════════════

    def _reset_turn_state(self, text: str, *, trigger_source: str) -> None:
        """Reset all per-turn _last_* attributes before _process_turn_inner.

        Preserves cross-turn state (_last_trace_id, _pending_outcome_update)
        but clears anything that should not leak into the next trace row.

        Args:
            text: Raw user message (used for outcome detection on previous turn).
            trigger_source: Trace v3 trigger_source enum value.
        """
        # System-test harness fields (legacy)
        self._last_route = None
        self._last_path = "unknown"
        self._last_device_ops = []
        self._last_memory_hits = ""
        self._last_timings = {}
        self._last_farewell_match = None
        self._last_memory_keyword = None
        self._last_escalation = None
        self._last_learning_intent = None
        self._last_keyword_rule = None
        self._last_direct_answer = None
        self._last_reqllm = False
        self._last_history_turns = 0
        self._last_tool_calls = []
        self._last_tool_iterations = 0
        self._last_llm_tokens = {}
        self._last_route_cache_hit = None
        self._last_provider_chain = []
        self._last_memory_retrieval = {}
        self._last_memory_extraction = {}

        # Trace v3 per-turn state
        self._last_trigger_source = trigger_source
        self._last_response_text = ""
        self._last_end_reason = None
        self._last_error = None
        self._last_intent_route_score = None
        self._last_tts_emotion = None
        self._last_memory_query_ids = None
        self._last_finish_reason = None
        self._last_cache_read_tokens = None
        self._last_updated_messages = None
        self._last_tool_call_log = None  # populated in cloud path via wrapped executor
        self._last_history_len_before = 0  # set by inner after history load
        # Voice-path captures default to None; _process_turn overrides them
        # immediately after reset using kwargs from _handle_utterance_inner.
        self._last_asr_ms = None
        self._last_asr_confidence = None
        self._last_vad_duration_ms = None
        self._last_llm_metadata = {
            "provider": None,
            "conv_id": None,
            "response_id": None,
            "streaming": False,
            "fallback_used": False,
            "truncated_by_interrupt": False,
            "full_response": None,
            "cache_creation_input_tokens": None,
        }
        self._first_audio_at = None

        # Outcome lag: detect_outcome on THIS turn judges the PREVIOUS turn.
        # Schedule the update; _flush_trace applies it after this turn logs.
        from memory.outcome_detector import detect_outcome
        signal = detect_outcome(text)
        if signal is not None and self._last_trace_id is not None:
            self._pending_outcome_update = (self._last_trace_id, signal)
        else:
            self._pending_outcome_update = None

    def _arm_tts_first_chunk(self) -> None:
        """Wire AudioStreamPlayer's on_first_chunk for the current turn.

        Two-tier strategy because the player is lazy-initialized inside
        TTSEngine — at turn-start we may not yet have a player object:

        1. Install the callback on TTSEngine via set_first_chunk_callback;
           the engine re-applies it whenever the player gets (re)constructed.
           Survives lazy init across all turns after the first audio call.

        2. If a player already exists, also reset its per-turn fired flag
           so the callback fires for THIS turn (would otherwise stay set
           from a prior turn and skip firing).

        Callback writes a single attribute (GIL-atomic) — no IO/logging,
        safe to run in PortAudio audio thread per audio_stream_player.py
        contract. Idempotent: safe to call on every turn even text-path.
        """
        # Stable closure: captures self via lexical scope. New closure each
        # turn so any stale reference held by the player is replaced.
        def _on_first_chunk() -> None:
            # Single attribute write under GIL — atomic per CPython.
            # Use perf_counter for monotonic resolution; turn_start in
            # _flush_trace also uses perf_counter so the diff is meaningful.
            self._first_audio_at = time.perf_counter()

        tts = getattr(self, "_tts", None)
        if tts is None:
            return  # TTS not initialized yet; first voice turn loses ttfs_ms
        # Tier 1: register on engine — engine forwards to player on creation.
        if hasattr(tts, "set_first_chunk_callback"):
            try:
                tts.set_first_chunk_callback(_on_first_chunk)
            except Exception:
                pass
        # Tier 2: if player exists right now, reset its per-turn flag so
        # the callback fires for this turn's first chunk.
        player = getattr(tts, "_stream_player", None)
        if player is not None:
            try:
                player.reset_first_chunk()
            except AttributeError:
                pass

    def _flush_trace(
        self,
        *,
        text: str,
        session_id: str,
        user_id: str,
        emotion: str,
        turn_start: float,
    ) -> None:
        """Assemble a v3 trace row from staged _last_* state and write it.

        Always runs in the outer wrapper's finally — covers normal returns,
        early returns (farewell / memory_l1 / etc.), and exceptions. Any
        failure here is logged and swallowed; trace logging must never
        bring down the conversation pipeline.
        """
        try:
            from memory.pricing import compute_cost_usd

            # Trace v3: session_id = per-launch app session, NOT user_id.
            # The conversation_store still keys history by user_id (passed
            # in as `session_id` arg here from legacy callers), but for trace
            # we want a launch-scoped session boundary so (session_id, turn_id)
            # is meaningful for analytics.
            trace_session = self._app_session_id
            session_turn = self._turn_counter.get(trace_session, 0) + 1
            self._turn_counter[trace_session] = session_turn

            # Total latency (turn_start was captured BEFORE _reset_turn_state)
            total_ms = int((time.monotonic() - turn_start) * 1000)
            self._last_timings["total_ms"] = total_ms

            # ttfs_ms: time from turn_start to first non-silent audio chunk.
            # NULL when no TTS played (text-only path, or early return without
            # output_fn audio dispatch).
            ttfs_ms: int | None = None
            if self._first_audio_at is not None:
                ttfs_ms = int((self._first_audio_at - turn_start) * 1000)
                if ttfs_ms < 0:
                    ttfs_ms = None  # clock skew guard

            # input_metadata: stable shape; emit None for absent fields.
            # asr_ms also stashed here on voice path (None on text path).
            input_metadata = {
                "asr_text_raw": None,
                "asr_confidence": self._last_asr_confidence,
                "vad_duration_ms": self._last_vad_duration_ms,
                "audio_path": None,
            }

            # tool_calls extraction:
            # 1. PREFER the wrapped-executor log (cloud path) — has real
            #    result + ms per call.
            # 2. ELSE fall back to scanning the messages added THIS turn
            #    (updated_messages[history_len_before:]) for tool_use blocks.
            #    NEVER scan the full updated_messages — that leaks few-shot
            #    examples and stale tool calls baked into history.
            tool_calls: list[dict] = []
            if self._last_tool_call_log:
                tool_calls = list(self._last_tool_call_log)
            elif self._last_updated_messages:
                _start_idx = self._last_history_len_before
                new_msgs = self._last_updated_messages[_start_idx:]
                for msg in new_msgs:
                    if msg.get("role") != "assistant":
                        continue
                    content = msg.get("content")
                    if not isinstance(content, list):
                        continue
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            tool_calls.append({
                                "name": block.get("name", ""),
                                "args": block.get("input", {}),
                                "result": "",
                                "ms": 0,
                            })

            # Decide which LLM produced this turn's response and pull
            # tokens / finish_reason / cost from the right source.
            #
            # Three cases:
            # (a) Cloud path: main LLMClient (self.llm). Metadata harvested
            #     in cloud finally via self._last_llm_metadata + tokens.
            # (b) Router-driven response (route.text_response set, path=
            #     local): the intent router's Groq/Cerebras LLM produced
            #     the response in a single call. Pull intent_router.
            #     last_metadata for tokens/finish_reason/model.
            # (c) Pure local (keyword_rule, farewell, memory_l1, memory_
            #     shortcut): no LLM ran. All llm_* stay NULL.
            llm_metadata = self._last_llm_metadata
            llm_tokens_in: int | None = None
            llm_tokens_out: int | None = None
            cache_read = self._last_cache_read_tokens
            cache_write = llm_metadata.get("cache_creation_input_tokens") if llm_metadata else None
            llm_model_used: str | None = None
            finish_reason_used = self._last_finish_reason

            if self._last_path == "cloud":
                # (a) Main chat LLM. Tokens were stashed in self._last_llm_tokens
                # (legacy) — _last_llm_metadata is the v3 source of truth but
                # tokens themselves live alongside (cache_read on its own attr).
                llm_model_used = self.llm.model
                llm_tokens_in = self._last_llm_tokens.get("input") if self._last_llm_tokens else None
                llm_tokens_out = self._last_llm_tokens.get("output") if self._last_llm_tokens else None
            elif self._last_route is not None and getattr(self._last_route, "text_response", None):
                # (b) Router-driven inline response. Pull from intent_router.
                router_meta = getattr(self.intent_router, "last_metadata", None) or {}
                llm_model_used = router_meta.get("model")
                llm_tokens_in = router_meta.get("tokens_in")
                llm_tokens_out = router_meta.get("tokens_out")
                cache_read = router_meta.get("cache_read_input_tokens") or cache_read
                if not finish_reason_used:
                    finish_reason_used = router_meta.get("finish_reason")
                # Augment llm_metadata to record router origin without
                # losing the dict shape locked by the schema.
                if llm_metadata is None:
                    llm_metadata = {}
                llm_metadata["provider"] = router_meta.get("provider")
                llm_metadata["response_id"] = router_meta.get("response_id")
                llm_metadata["streaming"] = False  # router uses single non-streaming HTTP
                llm_metadata["router_model"] = router_meta.get("model")
            # (c) llm_model_used stays None, no tokens.

            cost = compute_cost_usd(
                model=llm_model_used,
                tokens_in=llm_tokens_in,
                tokens_out=llm_tokens_out,
                cache_read_in=cache_read,
                cache_write_in=cache_write,
                pricing_table=self.config.get("llm_pricing", {}),
            )

            assistant_text = self._last_response_text or ""

            # Coerce empty strings to None for nullable string columns —
            # SQL semantics: "" is a value, NULL is absence. Analytics
            # queries on `IS NULL` should not match empty placeholders.
            user_emotion_val = emotion if emotion else None
            tts_emotion_val = self._last_tts_emotion if self._last_tts_emotion else None

            trace_id = self.trace_log.log_turn(
                session_id=trace_session,
                turn_id=session_turn,
                user_id=user_id,
                user_text=text,
                assistant_text=assistant_text,
                user_emotion=user_emotion_val,
                tts_emotion=tts_emotion_val,
                input_metadata=input_metadata,
                trigger_source=self._last_trigger_source,
                parent_trace_id=None,
                path_taken=self._last_path,
                intent_route_score=self._last_intent_route_score,
                tool_calls=tool_calls or None,
                llm_model=llm_model_used,
                llm_tokens_in=llm_tokens_in,
                llm_tokens_out=llm_tokens_out,
                cache_read_input_tokens=cache_read,
                llm_metadata=llm_metadata if llm_metadata else None,
                memory_query_ids=self._last_memory_query_ids,
                prompt_version=self._prompt_version,
                latency_ms=total_ms,
                ttfs_ms=ttfs_ms,
                latency_breakdown={
                    "asr_ms":          self._last_asr_ms,
                    "route_ms":        self._last_timings.get("route_ms"),
                    "memory_query_ms": self._last_timings.get("memory_query_ms"),
                    "direct_answer_ms": self._last_timings.get("direct_answer_ms"),
                    "local_exec_ms":   self._last_timings.get("local_exec_ms"),
                    "llm_first_ms":    self._last_timings.get("llm_first_ms"),
                    "tts_first_ms":    ttfs_ms,
                    "total_ms":        total_ms,
                },
                end_reason=self._last_end_reason,
                error=self._last_error,
                finish_reason=finish_reason_used,
                cost_usd=cost,
            )

            # Apply pending outcome update for the previous turn (lag-1 model).
            if self._pending_outcome_update is not None:
                prev_id, signal = self._pending_outcome_update
                self.trace_log.update_outcome(prev_id, signal=signal, at_turn_id=trace_id)
                self._pending_outcome_update = None

            # Carry this turn's id forward for next turn's outcome detection.
            self._last_trace_id = trace_id

            # Async Observer extraction (writes to observations table).
            self._executor.submit(
                self.memory_manager.write_observation,
                {
                    "user_text": text,
                    "assistant_text": assistant_text,
                    "tool_calls": tool_calls,
                    "user_emotion": emotion,
                },
                trace_id,
            )
        except Exception as exc:
            self.logger.warning("Trace flush failed (non-fatal): %s", exc)

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

    def _truncate_assistant_for_interrupt(
        self, messages: list[dict], played: list[str],
    ) -> list[dict]:
        """WP5 方案 b: shrink the last assistant message to only the heard
        sentences and append a [Interrupted by user] marker.

        Handles both OpenAI-style ``content: str`` and Anthropic-style
        ``content: list[block]`` shapes (provider differences are flattened
        elsewhere; we just preserve whichever shape is present).

        Args:
            messages: The full message list returned by ``llm.chat_stream``.
            played:   Sentences whose audio finished playing before abort —
                      i.e. what the user actually heard.

        Returns:
            New message list with the last assistant turn truncated and a
            trailing user marker appended.
        """
        if not messages:
            return messages
        truncated = list(messages)
        heard = "".join(played)
        new_content_str = (heard + "...") if heard else "..."
        for i in range(len(truncated) - 1, -1, -1):
            entry = truncated[i]
            if entry.get("role") != "assistant":
                continue
            existing = entry.get("content")
            if isinstance(existing, list):
                truncated[i] = {
                    **entry,
                    "content": [{"type": "text", "text": new_content_str}],
                }
            else:
                truncated[i] = {**entry, "content": new_content_str}
            break
        truncated.append({"role": "user", "content": "[Interrupted by user]"})
        self.logger.info(
            "WP5: history truncated to %d played sentence(s) + interrupted marker",
            len(played),
        )
        return truncated

    def _on_voice_resume(self) -> None:
        """Called by InterruptMonitor when a resume keyword is detected.

        Stops TTS playback so control returns to _process_turn, but does NOT
        clear _interrupted_response — the resume check in _process_turn needs it.
        """
        self.logger.info("Voice resume detected")
        self._cancel.set()
        # Stop playback but preserve _interrupted_response for replay
        pipeline: Any = None
        with self._pipeline_lock:
            pipeline = self._active_pipeline
        if pipeline:
            pipeline.abort()  # kills playback, don't save remaining
        tts = self._get_tts()
        if tts:
            tts.stop()

    # ------------------------------------------------------------------
    # WP7 soft-stop callbacks (VAD-driven pause/resume of TTS playback)
    # ------------------------------------------------------------------

    def _on_soft_pause(self) -> None:
        """Pause active TTS playback when VAD detects user speech."""
        tts = self._get_tts()
        if tts is None:
            return
        if tts.suspend_playback():
            self.logger.debug("Soft-pause: TTS suspended on VAD start")

    def _on_soft_resume(self) -> None:
        """Resume TTS playback when VAD ends or no-keyword timeout fires."""
        tts = self._get_tts()
        if tts is None:
            return
        if tts.resume_playback():
            self.logger.debug("Soft-resume: TTS resumed (no keyword in window)")

    def _cancel_current(self) -> None:
        """Cancel current TTS and reset state after user interrupt."""
        # Read pipeline ref under lock, call abort() outside to avoid blocking
        pipeline: Any = None
        with self._pipeline_lock:
            self._interrupted_response = None  # clear stale buffer first
            pipeline = self._active_pipeline
        if pipeline:
            remaining = pipeline.abort()
            self.event_bus.emit("jarvis.tts_cancelled", {"reason": "vad"})
            # WP5: snapshot played sentences for the surrounding _process_turn
            # to fold into the conversation history (see _truncate_assistant_for_interrupt).
            self._interrupt_played_texts = pipeline.played_texts
            if remaining:
                with self._pipeline_lock:
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

    def _warmup_interrupt_vad(self) -> None:
        """Warm up the interrupt monitor's VAD by triggering lazy load."""
        try:
            self.interrupt_monitor._load_vad()
            if self.interrupt_monitor._vad is not None:
                self.interrupt_monitor._vad.accept_waveform(
                    np.zeros(512, dtype=np.float32)
                )
        except Exception as exc:
            self.logger.warning("Interrupt VAD warmup failed: %s", exc)

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
        tool_count = self.tool_registry.count()
        print("=" * 60)
        print("  J.A.R.V.I.S. — Personal AI Voice Assistant")
        print("=" * 60)
        print(f"  Device mode : {mode}")
        print(f"  Users       : {user_count} registered")
        print(f"  Tools       : {tool_count}")
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
            try:
                result = self.tool_registry.execute("get_weather", {}, user_role="owner")
                if result and "unknown tool" not in result.lower():
                    parts.append(result)
            except Exception as exc:
                self.logger.warning("Briefing weather failed: %s", exc)

        if "reminders" in include:
            try:
                result = self.tool_registry.execute("list_reminders", {}, user_role="owner")
                if result and "No active" not in result and "unknown tool" not in result.lower():
                    parts.append(result)
            except Exception as exc:
                self.logger.warning("Briefing reminders failed: %s", exc)

        if "todos" in include:
            try:
                result = self.tool_registry.execute("list_todos", {}, user_role="owner")
                if result and "No " not in result and "unknown tool" not in result.lower():
                    parts.append(result)
            except Exception as exc:
                self.logger.warning("Briefing todos failed: %s", exc)

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
