# REVIEW [B01] L1-49 ACTIVE · 模块顶部 (docstring + imports + 警告抑制)
# REVIEW 分析: stdlib/3rd-party/项目内部 import；urllib3+ONNX 警告抑制 L26-31 跟 import 块混；L34 `Action,ActionResponse` 仅服务下面 REQLLM 死分支
# REVIEW 建议: 留主体；L34 import 等 IPB9 死分支删后同步移除；L26-31 顺序整理可作 Tier 3 小修
# REVIEW 评估: ___
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
from core.tts import SentenceType
from auth.user_store import UserStore
from core.audio_recorder import AudioRecorder
from core.event_bus import EventBus
from core.llm import LLMClient
from core.speaker_encoder import SpeakerEncoder
from core.speaker_verifier import SpeakerVerifier
from core.speech_recognizer import SpeechRecognizer
from devices.device_manager import DeviceManager
from memory.hot.conversation import ConversationStore
from memory.manager import MemoryManager
from memory import trace as memory_trace
from core.tool_registry import ToolRegistry

LOGGER = logging.getLogger(__name__)

# REVIEW [B02] L51-54 MIXED · 模块常量
# REVIEW 分析: L52 `_ESCALATION_KEYWORDS` ACTIVE (用在 _process_turn_inner L893)；L54 `_NEEDS_LLM_ACTIONS = {"set_effect"}` DEAD (无 reader)
# REVIEW 建议: 留 L52；删 L53-54 整段
# REVIEW 评估: ___
# 用户说这些前缀词，本轮临时切换到更强的 LLM 模型（deep preset）
_ESCALATION_KEYWORDS = ("仔细想想", "详细分析", "认真想", "好好想")
# 这些智能家居动作无法本地解析参数，必须交给 LLM 理解
_NEEDS_LLM_ACTIONS = {"set_effect"}


# REVIEW [B03] L57-78 DEAD · _color_needs_llm 模块级 helper
# REVIEW 分析: 22 行函数无 caller (grep 全 repo 仅定义自身)；原服务 intent_router/local_executor smart_home 路径
# REVIEW 建议: 删整个函数
# REVIEW 评估: ___
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

# REVIEW [B04] L81-87 ACTIVE · APScheduler 模块级 health helpers
# REVIEW 分析: `_health_tracker_ref` + `_run_health_probes` 模块级 (APScheduler 序列化 job 必须用模块级 ref，不能 self.method)
# REVIEW 建议: 留
# REVIEW 评估: ___
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

        # REVIEW [IB1] L102-112 ACTIVE · 事件总线 + health_tracker
        # REVIEW 分析: EventBus 组件解耦 (state_changed/health.status_changed/response.start 等)；health_tracker 可选，config.health.enabled 默认 true
        # REVIEW 建议: 留
        # REVIEW 评估: ___
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

        # REVIEW [IB2] L114-130 ACTIVE · 音频+身份 stack + LLM/对话/记忆
        # REVIEW 分析: UserStore/AudioRecorder/SpeakerEncoder/Verifier/SpeechRecognizer/ASRNormalizer/DeviceManager/PermissionManager + LLMClient/ConversationStore/MemoryManager
        # REVIEW 建议: 留
        # REVIEW 评估: ___
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

        # ── 长期记忆 ── SQLite + 向量嵌入
        self.memory_manager = MemoryManager(config)

        # REVIEW [IB3] L132-186 ACTIVE · 行为日志 + Trace v3 全套设施
        # REVIEW 分析: BehaviorLog+TraceLog 共用 SQLite (WAL)；turn_counter/pricing/NLI lazy/app_session_id (per-launch)/prompt_version (sha256 personality.py)/cross-turn placeholders/voice-path captures
        # REVIEW 建议: 留
        # REVIEW 评估: ___
        from memory.cold.behavior_log import BehaviorLog
        mem_db = config.get("memory", {}).get("db_path", "data/memory/jarvis_memory.db")
        # BehaviorLog 和 MemoryManager 共用同一个 SQLite（不同表），WAL 模式支持并发读写
        self.behavior_log = BehaviorLog(mem_db)

        from memory.trace import TraceLog
        self.trace_log = TraceLog(mem_db)
        self._turn_counter: dict[str, int] = {}  # session_id → turn count

        from memory.cold.pricing import load_pricing_table
        self._pricing_table = load_pricing_table()

        from memory.cold.nli_classifier import NLIClassifier
        nli_cfg = config.get("memory", {}).get("outcome_detector", {}).get("nli", {})
        self.nli_classifier = NLIClassifier(
            model_dir=nli_cfg.get("model_dir", "data/nli-erlangshen"),
            entailment_threshold=nli_cfg.get("entailment_threshold", 0.65),
            min_text_length=nli_cfg.get("min_text_length", 2),
            max_text_length=nli_cfg.get("max_text_length", 500),
        )  # lazy-loaded on first use

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
        #   async NLI outcome resolution in _flush_trace to update the
        #   PREVIOUS turn (the one the current turn is judging).
        self._last_trace_id: int | None = None

        # ── Trace v3 voice-path captures (set per turn via _process_turn kwargs) ──
        # Reset every turn in _reset_turn_state. Stay None for text-only path.
        self._last_asr_confidence: float | None = None
        self._last_vad_duration_ms: int | None = None
        self._last_asr_ms: int | None = None
        self._last_audio_path: str | None = None

        # REVIEW [IB4] L188-207 ACTIVE · 音频归档 + last user/session 占位
        # REVIEW 分析: audio capture (config 控制开关) + first_audio_at + last_turn_end_at + last_user_id/session_id；future quality work (n-best/对比) 数据源
        # REVIEW 建议: 留
        # REVIEW 评估: ___
        # Audio capture: archive raw 16k float32 wav per voice turn so future
        # quality work (n-best, model comparison, fine-tuning data) is unblocked.
        _capture_cfg = config.get("audio", {}).get("capture", {})
        self._audio_capture_enabled = bool(_capture_cfg.get("enabled", False))
        self._audio_capture_dir = str(_capture_cfg.get("dir", "data/audio"))
        # Time (perf_counter) at which AudioStreamPlayer first emitted real
        # samples for the current turn. Set by the on_first_chunk callback
        # (audio thread). Single attr-assign is GIL-atomic, no lock needed.
        self._first_audio_at: float | None = None
        # Trace v3: snapshots set by _mark_turn_end at the actual end-of-turn
        # moment. _flush_trace prefers these over computing fresh values in
        # the wrapper's finally — that decouples end_reason / latency_ms
        # from the wrapper-finally moment, avoiding a race where the next
        # turn's user input sets _cancel before this turn's flush runs.
        self._last_turn_end_at: float | None = None
        self._last_real_end_reason: str | None = None

        # 记录最近一次交互的用户，farewell 时用来触发记忆保存
        self._last_user_id: str | None = None
        self._last_session_id: str | None = None

        # REVIEW [IB5] L209-228 ACTIVE · 可选子系统 (scheduler + OLED)
        # REVIEW 分析: APScheduler 给 reminders/health probes/morning briefing 用；OLED 给 RPi 显示屏，桌面 macOS 上跳过
        # REVIEW 建议: 留
        # REVIEW 评估: ___
        # ── 定时任务（可选）── 早报、记忆维护等 cron 任务
        self.scheduler = None
        try:
            from core.scheduler import JarvisScheduler
            self.scheduler = JarvisScheduler(config, self.event_bus)
            if self.scheduler.available and config.get("scheduler", {}).get("enabled", True):
                self.scheduler.start()
                self._setup_morning_briefing(config)
        except Exception as exc:
            self.logger.warning("Scheduler unavailable: %s", exc)

        # ── OLED 显示屏（可选）── RPi 上的小屏幕，显示状态/正在说的话
        self.oled = None
        if config.get("oled", {}).get("enabled", False):
            try:
                from ui.oled_display import OledDisplay
                self.oled = OledDisplay(config, self.event_bus)
                self.oled.start()
            except Exception as exc:
                self.logger.warning("OLED display unavailable: %s", exc)

        # REVIEW [IB6] L230-247 ACTIVE · tools init + ToolRegistry
        # REVIEW 分析: smart_home/time_utils/reminders/todos 各自 init() 注入依赖；ToolRegistry 自动扫 skills/+skills/learned/
        # REVIEW 建议: 留
        # REVIEW 评估: ___
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

        # L0 RegexRouter — fast-path classifier (17 patterns ^...$).
        # L1 surrogate slot reserved for trace-trained classifier (see TraceLog).
        self.regex_router = None
        try:
            from core.regex_router import RegexRouter
            self.regex_router = RegexRouter(config)
        except Exception as exc:
            self.logger.warning("RegexRouter unavailable: %s", exc)
        # Latest regex match (set during _process_turn when L0 hits) — used by
        # _flush_trace to record regex_pattern_id without re-running the match.
        self._last_regex_match = None

        # REVIEW [IB8] L267-283 ACTIVE · InterruptMonitor + TTS placeholder
        # REVIEW 分析: InterruptMonitor 共享 main-path SpeechRecognizer + ASRNormalizer (避免双 load)；TTS 懒加载占位 (首次调用才 init)
        # REVIEW 建议: 留
        # REVIEW 评估: ___
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

        # REVIEW [IB9] L285-300 MIXED · 会话状态
        # REVIEW 分析: _cancel/utterance_duration/silence_timeout/ThreadPoolExecutor(3)/_pipeline_lock 全 ACTIVE；L295 `_interrupted_response` 写入但 resume read 路径已删 (commit 4692e3d)，无 reader → DEAD state；L300 `_interrupt_played_texts` 是 WP5 仍活
        # REVIEW 建议: 删 `_interrupted_response: list[str] | None = None` 占位 + B32 里两处写入 + 3 个 test 文件引用 (Tier 2 #6)
        # REVIEW 评估: ___
        # ── 会话状态 ──
        self._cancel = threading.Event()  # 用户按 Enter 打断时设置此信号
        session_config = config.get("session", {})
        self.silence_timeout = float(session_config.get("silence_timeout", 30))
        self.utterance_duration = float(session_config.get("utterance_duration", 5))
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

        # REVIEW [IB10] L302-324 ACTIVE · prewarms + health monitor wire-up
        # REVIEW 分析: 4 个 prewarm submitted 到 executor (HTTP keepalive/TTS precache 6 短句/ASR/VAD)；health 状态变化订阅 + 定期 probe
        # REVIEW 建议: 留
        # REVIEW 评估: ___
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
        groq_key = self.config.get("models", {}).get("groq", {}).get("api_key")
        if groq_key:
            try:
                _req.get(
                    "https://api.groq.com/openai/v1/models",
                    headers={"Authorization": f"Bearer {groq_key}"},
                    timeout=5,
                )
            except Exception:
                pass

    # REVIEW [B12] L339-343 ACTIVE · _on_health_changed
    # REVIEW 分析: degradation 第一次发生时语音通知"X 暂时不可用，已切换备用"
    # REVIEW 建议: 留
    # REVIEW 评估: ___
    def _on_health_changed(self, data: dict) -> None:
        """Voice-notify user on first degradation only."""
        if data.get("new_status") == "degraded" and data.get("old_status") == "healthy":
            component = data.get("component", "unknown")
            self.speak(f"{component} 暂时不可用，已切换备用。")

    # REVIEW [B13] L345-406 ACTIVE · _setup_health_probes
    # REVIEW 分析: 注册 groq/openai/asr 三个 probe + scheduler interval (默认 60s)；用模块级 _health_tracker_ref 因 APScheduler 序列化要求
    # REVIEW 建议: 留
    # REVIEW 评估: ___
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

    # REVIEW [B14] L408-416 ACTIVE · shutdown
    # REVIEW 分析: executor.shutdown(wait=True) → scheduler.stop() → oled.stop() → mqtt.disconnect()；最后一行用了 `device_manager._mqtt_client` 私有属性 (Tier 3 cosmetic)
    # REVIEW 建议: 留
    # REVIEW 评估: ___
    def shutdown(self) -> None:
        """Clean up all subsystems."""
        self._executor.shutdown(wait=True)
        if self.scheduler and self.scheduler.available:
            self.scheduler.stop()
        if self.oled:
            self.oled.stop()
        if hasattr(self.device_manager, '_mqtt_client') and self.device_manager._mqtt_client:
            self.device_manager._mqtt_client.disconnect()

    # REVIEW [B15] L418-502 ACTIVE · run_interactive (--no-wake 模式)
    # REVIEW 分析: 后台线程 _input_listener 监 Enter；主循环录音→handle_utterance→检 _cancel→检 farewell；按 farewell 用 `_last_path == "farewell"` 判退出
    # REVIEW 建议: 留
    # REVIEW 评估: ___
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

                    if self._last_path == "farewell":
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

    # REVIEW [B16] L504-590 ACTIVE · run_always_listening (wake word 模式)
    # REVIEW 分析: WakeWordDetector(openwakeword) + sd.InputStream 双流抢麦克风方案；hit→speak("在的")→暂停 wake stream→record→handle_utterance→恢复；farewell 走 reset+drain+listening_for_wake=True
    # REVIEW 建议: 留 (注：docstring 提 Porcupine 但实际是 openwakeword，可改)
    # REVIEW 评估: ___
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

                        if self._last_path == "farewell":
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

    # REVIEW [B17] L592-606 ACTIVE · handle_utterance (顶层 wrapper)
    # REVIEW 分析: try/except 捕全部 pipeline 异常，emit state=idle 后重新抛
    # REVIEW 建议: 留
    # REVIEW 评估: ___
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

    # REVIEW [B18] L608-702 ACTIVE · _handle_utterance_inner (语音入口)
    # REVIEW 分析: 并行 SpeakerVerifier+SpeechRecognizer→snapshot ASR/VAD/audio metadata→audio capture→resolve user→_voice_output→_process_turn(trigger_source="wake_word")；空 ASR 早返 "" 不写 trace
    # REVIEW 建议: 留
    # REVIEW 评估: ___
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

        # Archive raw audio. Best-effort: failures must not poison the turn.
        _audio_path: str | None = None
        if self._audio_capture_enabled and audio.size > 0:
            try:
                ts = datetime.now().strftime("%Y%m%dT%H%M%S_%f")[:-3]
                rel = Path(self._audio_capture_dir) / self._app_session_id / f"{ts}.wav"
                rel.parent.mkdir(parents=True, exist_ok=True)
                self.audio_recorder.save_wav(audio, str(rel))
                _audio_path = str(rel)
            except Exception:
                self.logger.warning("audio capture failed", exc_info=True)
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
            audio_path=_audio_path,
        )

        _t_end = time.monotonic()
        print(f"⏱ 总耗时: {(_t_end - _t0)*1000:.0f}ms")

        return response_text

    # ══════════════════════════════════════════════════════════════
    # 核心处理流水线（语音和文本两条入口共用）
    # ══════════════════════════════════════════════════════════════

    # REVIEW [B19] L708-822 MIXED · _process_turn (外层 wrapper)
    # REVIEW 分析: try/finally 包 _flush_trace + inherent card 事件转发；STALE-DOC L781-786 注释提"farewell, memory_shortcut, resume, fall-through"，后三个已删
    # REVIEW 建议: 留逻辑；改 L781-786 注释只列 regex/farewell/cloud
    # REVIEW 评估: ___
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
        audio_path: str | None = None,
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
        turn_start_perf = time.perf_counter()
        # Per-turn capture cell for ttfs_ms — fresh list so a deferred
        # async-TTS-done callback for THIS turn won't be clobbered by
        # the next turn's reset. Mutated by the audio thread.
        ttfs_cell: list[int | None] = [None]
        self._reset_turn_state(text, trigger_source=trigger_source)
        # Stash voice-path snapshots after the reset (these would otherwise
        # be clobbered by reset_turn_state's defaults).
        self._last_asr_ms = asr_ms
        self._last_asr_confidence = asr_confidence
        self._last_vad_duration_ms = vad_duration_ms
        self._last_audio_path = audio_path
        # Trace v3: arm the AudioStreamPlayer first-chunk callback BEFORE
        # the inner runs. Captures into the per-turn ttfs_cell so a
        # deferred TTS-done update can read it without racing reset.
        self._arm_tts_first_chunk(turn_start_perf=turn_start_perf, cell=ttfs_cell)
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
                # Prefer the inner's snapshot taken at actual end-of-turn —
                # avoids racing with the NEXT turn's user input setting
                # _cancel between inner return and this finally.
                if self._last_real_end_reason:
                    self._last_end_reason = self._last_real_end_reason
                else:
                    self._last_end_reason = (
                        "interrupted" if self._cancel.is_set() else "success"
                    )
            # Inherent card events. Cloud streaming path already emitted
            # response.start + per-sentence response.chunk via _on_sentence;
            # here we only need response.final. Non-streaming paths (regex,
            # farewell, memory_shortcut, resume, fall-through) reach here
            # without any prior emit — synthesize start + full chunk + final
            # so the card sees the same protocol regardless of source.
            # NB: deliberately NOT using `text` as the local name — the finally
            # block below calls _flush_trace(text=text, ...) with the user
            # input parameter; shadowing it would corrupt user_text in trace.
            _card_text = self._last_response_text
            if _card_text:
                if not self._last_card_streamed_emitted:
                    self.event_bus.emit("response.start", {"path": self._last_path})
                    self.event_bus.emit("response.chunk", {"text": _card_text})
                self.event_bus.emit(
                    "response.final",
                    {"text": _card_text, "path": self._last_path},
                )
            return result
        except Exception as exc:
            self._last_end_reason = "error"
            self._last_error = (
                f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[:2000]}"
            )
            raise
        finally:
            flushed_id: int | None = None
            if user_id:
                flushed_id = self._flush_trace(
                    text=text,
                    session_id=session_id,
                    user_id=user_id,
                    emotion=emotion,
                    turn_start=turn_start,
                    ttfs_ms_known=ttfs_cell[0],
                )
            # Defer ttfs_ms write for async local-path TTS that started
            # in output_fn but hasn't played yet. Best-effort, no-op if
            # already captured or no pending audio.
            if flushed_id is not None and ttfs_cell[0] is None:
                self._defer_ttfs_update(flushed_id, ttfs_cell)

    # REVIEW [B20] L823-1211 MIXED · _process_turn_inner (主流水线)
    # REVIEW 分析: 13 子段 (IPB1-13)；STALE-DOC L837-839 docstring 列 7 个已删 path (farewell/memory_shortcut/learning/keyword_trigger/direct_answer/intent_route/route_dispatch)
    # REVIEW 建议: 改 docstring 只列 regex+cloud；逻辑见各 IPB# 子标
    # REVIEW 评估: ___
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
        # REVIEW [IPB1] L856-863 ACTIVE · 清 stale interrupt-played state (P0-B)
        # REVIEW 分析: 防上轮 cancel 留下的 _interrupt_played_texts 漏到下轮 mistruncate；_cancel_current 中途会重写
        # REVIEW 建议: 留
        # REVIEW 评估: ___
        # P0-B: clear stale interrupt-played state from any prior turn.
        # Non-cloud_path returns (resume / farewell / etc.)
        # don't consume _interrupt_played_texts, so without this reset a
        # later cloud_path turn would mis-truncate its assistant response
        # against the previous interrupt's played sentences.
        # _cancel_current re-populates this attribute mid-turn if the user
        # interrupts during streaming, and the consumer below clears it again.
        self._interrupt_played_texts = None

        # REVIEW [IPB2] L865-872 ACTIVE · ASR normalize (T1.5)
        # REVIEW 分析: 共享 normalizer 让 web/text path 也吃 corrections；require_context 守卫保证 voice-only 修正不污染文本
        # REVIEW 建议: 留
        # REVIEW 评估: ___
        # T1.5: normalize on shared pipeline so text-path (handle_text/web/MQTT)
        # also benefits. Corrections have require_context guards → safe for
        # non-voice input.
        _raw_text = text
        self._last_asr_text_raw = _raw_text
        text = self.asr_normalizer.normalize(text)
        if text != _raw_text:
            self.logger.info("ASR normalized: %r -> %r", _raw_text, text)

        # REVIEW [IPB3] L874-888 ACTIVE · history 加载 + trace v3 snapshot
        # REVIEW 分析: ConversationStore.get_history(session_id)→snapshot _last_history_len_before 给 _flush_trace 用 (避免 leak few-shot 到 tool_calls)
        # REVIEW 建议: 留
        # REVIEW 评估: ___
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

        # REVIEW [IPB4] L890-910 ACTIVE · escalation (升级模式)
        # REVIEW 分析: 4 个前缀词触发 LLM "deep" preset 切换；voice path 加一句"嗯，让我想想"占空白
        # REVIEW 建议: 留
        # REVIEW 评估: ___
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

        # ── per-turn 局部状态 ──
        _t_think = time.monotonic()
        self.event_bus.emit("jarvis.state_changed", {"state": "thinking"})
        response_text = None
        updated_messages = None
        sentence_count = 0

        # REVIEW [IPB6] L921-942 ACTIVE · 记忆检索 (4-block prompt 装配)
        # REVIEW 分析: memory_manager.build_prompt_context 返回 PromptContext (含 injected_observation_ids)；耗时 50-100ms
        # REVIEW 建议: 留
        # REVIEW 评估: ___
        # ── 记忆检索 ── Assembler 装配 4-block prompt（~50-100ms）
        prompt_ctx = None
        if user_id:
            _t_mem_start = time.monotonic()
            try:
                prompt_ctx = self.memory_manager.build_prompt_context(
                    text=text,
                    user_id=user_id,
                    history=history,
                    user_name=user_name,
                    user_role=user_role,
                    user_emotion=emotion,
                    situation="normal",
                )
                _mem_ms = int((time.monotonic() - _t_mem_start) * 1000)
                self._last_memory_hits = prompt_ctx
                self._last_timings["memory_query_ms"] = _mem_ms
                _obs_count = len(prompt_ctx.injected_observation_ids)
                if _obs_count:
                    print(f"🧠 记忆检索: {_obs_count} 条观察 ({_mem_ms}ms)")
            except Exception as exc:
                self.logger.warning("Memory query failed: %s", exc)

        # REVIEW [IPB7] L944-996 ACTIVE · L0 RegexRouter 快速路径
        # REVIEW 分析: regex_match→tool_registry.execute (no-op pattern 走 tool_result="")→render_response；farewell intent 设 _last_path="farewell"，其他设 "regex"；smart_home_control 记 _last_device_ops 给 trace
        # REVIEW 建议: 留
        # REVIEW 评估: ___
        # ── L0 Regex 快速路径 ──
        # 命中 → tool_registry.execute → render TTS 模板 → set response_text + path="regex"
        # 未命中 → response_text 保持 None，下游 cloud LLM (tool_use) 兜底
        regex_match = None
        if response_text is None and self.regex_router is not None:
            try:
                regex_match = self.regex_router.match(text)
            except Exception as exc:
                self.logger.warning("RegexRouter.match failed: %s", exc)
                regex_match = None

        _t_route = time.monotonic()
        _route_ms = int((_t_route - _t_think) * 1000)
        self._last_regex_match = regex_match
        self._last_route = None  # legacy field — None when regex path is used
        self._last_timings["route_ms"] = _route_ms

        if regex_match is not None:
            print(f"⏱ 路由: {_route_ms}ms → regex/{regex_match.intent} ({regex_match.pattern_id})")
            if regex_match.tool_name:
                print(f"   📋 {regex_match.tool_name} args={regex_match.tool_args}")
            try:
                # tool_name="" marks no-op patterns (e.g. farewell) — render
                # the template with empty tool_result, no tool_registry call.
                if regex_match.tool_name:
                    tool_result = self.tool_registry.execute(
                        regex_match.tool_name,
                        regex_match.tool_args,
                        user_role=user_role,
                    )
                else:
                    tool_result = ""
                response_text = self.regex_router.render_response(regex_match, tool_result)
                if regex_match.intent == "farewell":
                    self._last_path = "farewell"
                    self._last_farewell_match = text.strip().lower()
                else:
                    self._last_path = "regex"
                # Track device ops for trace continuity with old smart_home path.
                if regex_match.intent == "smart_home_control":
                    self._last_device_ops = [dict(regex_match.tool_args)]
                self.logger.info(
                    "Regex hit: %s → %s args=%s",
                    regex_match.pattern_id, regex_match.tool_name, regex_match.tool_args,
                )
            except Exception as exc:
                self.logger.warning("Regex tool execution failed: %s", exc)
                # On exec failure, fall through to cloud LLM
                regex_match = None
                self._last_regex_match = None
        else:
            print(f"⏱ 路由: {_route_ms}ms → miss (cloud LLM)")

        # REVIEW [IPB8] L997-1002 ACTIVE · 本地执行计时
        # REVIEW 分析: 仅 regex 命中时记 local_exec_ms，给 trace 看 fast-path 耗时
        # REVIEW 建议: 留
        # REVIEW 评估: ___
        # 本地执行计时
        if response_text is not None:
            _t_local = time.monotonic()
            _local_ms = int((_t_local - _t_route)*1000)
            print(f"⏱ 本地执行: {_local_ms}ms")
            self._last_timings["local_exec_ms"] = _local_ms

        # REVIEW [IPB9] L1004-1152 MIXED · 云端 LLM 路径
        # REVIEW 分析: wrapped_tool_executor 给 trace 抓 name/args/result/ms；TTSPipeline.prewarm + InterruptMonitor.start()→chat_stream(on_sentence)→finally 收尾；DEAD 子分支 L1084-1102 REQLLM 转述 (use_llm_rephrase=False 永远不 fire)；STALE-DOC L1004-1006 注释提"REQLLM 转述"
        # REVIEW 建议: 删 L1084-1102 整个 if 分支 (留 L1104 起的 else 体并 dedent)；改 L1004-1006 注释只留"完整 cloud"
        # REVIEW 评估: ___
        # ── 云端 LLM ── regex 未命中才到这里：streaming + tool-use 循环
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
                    # Inherent card: open stream on first sentence
                    self.event_bus.emit("response.start", {"path": self._last_path})
                    self._last_card_streamed_emitted = True
                # Inherent card: stream this sentence as a chunk (frontend drip
                # smooths sentence-burst cadence into per-char reveal)
                self.event_bus.emit("response.chunk", {"text": sentence})
                print(f"🤖 Jarvis: {sentence}")
                if tts_pipeline:
                    st = SentenceType.FIRST if sentence_count == 1 else SentenceType.MIDDLE
                    tts_pipeline.submit(sentence, st, emotion=emotion)
                    self._last_tts_chars_synthesized += len(sentence)
                else:
                    self._wait_tts()
                    output_fn(sentence)

            self.event_bus.emit("jarvis.state_changed", {"state": "speaking"})

            try:
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
                        prompt_context=prompt_ctx,
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
                    # Mirror tokens into _last_llm_tokens dict for the
                    # legacy shape that _flush_trace and other consumers
                    # already read. Sourced from new LLMClient properties.
                    self._last_llm_tokens = {
                        "input": getattr(self.llm, "last_input_tokens", None),
                        "output": getattr(self.llm, "last_output_tokens", None),
                    }
                except AttributeError:
                    pass  # older llm.py without v3 metadata exposure
                # Trace v3: snapshot the actual end-of-turn moment NOW —
                # AFTER tts_pipeline.wait_done() returns (TTS audio fully
                # played). _flush_trace would otherwise read _cancel state
                # at wrapper-finally time, which can race with the next
                # turn's user input setting _cancel. See _mark_turn_end.
                self._mark_turn_end()

        # REVIEW [IPB10] L1154-1175 ACTIVE · 持久化 conversation
        # REVIEW 分析: cloud path 用 updated_messages.replace；local path append+replace；WP5 interrupt 触发时 _truncate_assistant_for_interrupt 改写最后 assistant 消息；STALE-DOC 注释提 "GPT-4o-mini"，实际 Observer 用 Grok+Gemini 兜底 (CLAUDE.md)
        # REVIEW 建议: 留逻辑；改注释 LLM 名
        # REVIEW 评估: ___
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
        elif response_text:
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": response_text})
            self.conversation_store.replace(session_id, history)
            updated_messages = history
        # Trace v3: stash final messages for _flush_trace to extract tool_calls.
        self._last_updated_messages = updated_messages

        # REVIEW [IPB11] L1176-1193 ACTIVE · behavior_log
        # REVIEW 分析: 抽 tool_use blocks 记 skill_call；记 conversation 摘要 + route (local/cloud)
        # REVIEW 建议: 留 (注：跟 trace_log 字段重叠，未来可考虑合并)
        # REVIEW 评估: ___
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

        # REVIEW [IPB12] L1194-1198 ACTIVE · 非流式输出 fallback
        # REVIEW 分析: regex/farewell 路径 sentence_count==0，到这里一次性 output_fn(response_text)；含 OLED 展示
        # REVIEW 建议: 留
        # REVIEW 评估: ___
        # ── 非流式输出 ── 本地路径的响应在这里一次性播报（流式的已经在上面逐句播了）
        if sentence_count == 0 and not self._cancel.is_set() and response_text:
            if self.oled:
                self.oled.set_speaking_text(response_text)
            output_fn(response_text)

        # REVIEW [IPB13] L1200-1211 ACTIVE · 收尾 (恢复 preset + path 打印 + return)
        # REVIEW 分析: escalation 完毕回原 preset；打印 path=...；_mark_turn_end 幂等 snapshot (防 race)
        # REVIEW 建议: 留
        # REVIEW 评估: ___
        # 如果本轮升级了模型，恢复原来的 preset
        if _escalated and _original_preset is not None:
            try:
                self.llm.switch_model(_original_preset)
                self.logger.info("Restored preset to '%s' after escalation", _original_preset)
            except Exception as exc:
                self.logger.warning("Failed to restore preset: %s", exc)

        print(f"path={self._last_path}")
        self._last_response_text = response_text or ""
        self._mark_turn_end()
        return response_text

    # ══════════════════════════════════════════════════════════════
    # 纯文本入口（Web 前端用，不走录音/TTS）
    # ══════════════════════════════════════════════════════════════

    # REVIEW [B21] L1217-1245 ACTIVE · handle_text (Web 文本入口)
    # REVIEW 分析: 不走 ASR/TTS 的 wrapper；trigger_source="web_text"；TTSPipeline 不创建
    # REVIEW 建议: 留
    # REVIEW 评估: ___
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

    # REVIEW [B22] L1251-1318 MIXED · _reset_turn_state
    # REVIEW 分析: 每轮清 _last_*；7 个字段永远不被 set 是死的：_last_route (L1262)/_last_intent_route_score (L1290)/_last_reqllm (L1271)/_last_learning_intent (L1270)/_last_tool_iterations (L1274)/_last_memory_retrieval (L1276)/_last_memory_extraction (L1277)
    # REVIEW 建议: 删上述 7 行 (与 B26 _flush_trace 中读它们的代码同步删)
    # REVIEW 评估: ___
    def _reset_turn_state(self, text: str, *, trigger_source: str) -> None:
        """Reset all per-turn _last_* attributes before _process_turn_inner.

        Preserves cross-turn state (_last_trace_id)
        but clears anything that should not leak into the next trace row.

        Args:
            text: Raw user message (used for outcome detection on previous turn).
            trigger_source: Trace v3 trigger_source enum value.
        """
        # System-test harness fields (legacy)
        self._last_route = None
        self._last_regex_match = None
        self._last_path = "unknown"
        self._last_device_ops = []
        self._last_memory_hits = ""
        self._last_timings = {}
        self._last_farewell_match = None
        self._last_escalation = None
        self._last_learning_intent = None
        self._last_reqllm = False
        self._last_history_turns = 0
        self._last_tool_calls = []
        self._last_tool_iterations = 0
        self._last_llm_tokens = {}
        self._last_memory_retrieval = {}
        self._last_memory_extraction = {}

        # Trace v3 per-turn state
        self._last_trigger_source = trigger_source
        self._last_response_text = ""
        # Inherent card: tracks whether _on_sentence already emitted start +
        # chunks for this turn. Outer wrapper checks this to decide whether to
        # synthesize start+chunk(full) for non-streaming paths or just emit final.
        self._last_card_streamed_emitted = False
        self._last_end_reason = None
        self._last_turn_end_at = None
        self._last_real_end_reason = None
        self._last_error = None
        self._last_intent_route_score = None
        self._last_tts_emotion = None
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
        self._last_audio_path = None
        self._last_asr_text_raw: str | None = None
        self._last_tts_chars_synthesized = 0
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

        # Outcome lag: NLI runs async in _flush_trace on the PREVIOUS turn's
        # user_text. Nothing to do here — main path stays 0ms overhead.

    # REVIEW [B23] L1320-1343 ACTIVE · _mark_turn_end (幂等 end-of-turn snapshot)
    # REVIEW 分析: first-call wins；cloud 路径在 wait_done() 后调一次 (L1152)；inner 收尾再调一次 (L1210，兜底非 cloud 路径)；avoid race with next turn's _cancel
    # REVIEW 建议: 留 (双调用是有意防 race)
    # REVIEW 评估: ___
    def _mark_turn_end(self) -> None:
        """Snapshot end-of-turn moment for trace v3.

        Idempotent — only the FIRST call wins per turn. Captures:
        - ``_last_turn_end_at`` (time.monotonic snapshot used by
          _flush_trace for ``latency_ms``)
        - ``_last_real_end_reason`` ("interrupted" if ``_cancel`` is set
          right now, else "success") — _flush_trace prefers this over
          re-reading ``_cancel`` in the wrapper's finally, where a late
          interrupt from the NEXT turn's user input would otherwise
          poison this turn's record.

        Called from each return-bearing path inside _process_turn_inner
        (early returns + cloud finally after wait_done). The wrapper's
        existing fallback handles paths that didn't reach a snapshot
        (e.g., exceptions raised before any return).
        """
        if self._last_turn_end_at is not None:
            return  # first call wins; subsequent ignored
        self._last_turn_end_at = time.monotonic()
        if self._last_real_end_reason is None:
            self._last_real_end_reason = (
                "interrupted" if self._cancel.is_set() else "success"
            )

    # REVIEW [B24] L1345-1404 ACTIVE · _arm_tts_first_chunk (TTFS callback 装配)
    # REVIEW 分析: two-tier — engine 注册 first_chunk_callback (lazy player 创建后转发) + player 已存在则直接 reset_first_chunk；GIL-atomic 单 store 进 ttfs_cell
    # REVIEW 建议: 留
    # REVIEW 评估: ___
    def _arm_tts_first_chunk(
        self,
        *,
        turn_start_perf: float,
        cell: list[int | None],
    ) -> None:
        """Wire AudioStreamPlayer's on_first_chunk for the current turn.

        Two-tier strategy because the player is lazy-initialized inside
        TTSEngine — at turn-start we may not yet have a player object:

        1. Install the callback on TTSEngine via set_first_chunk_callback;
           the engine re-applies it whenever the player gets (re)constructed.
           Survives lazy init across all turns after the first audio call.

        2. If a player already exists, also reset its per-turn fired flag
           so the callback fires for THIS turn (would otherwise stay set
           from a prior turn and skip firing).

        Callback writes one int into ``cell`` (GIL-atomic single store) —
        no IO/logging, safe to run in PortAudio audio thread per
        audio_stream_player.py contract. ``cell`` is a per-turn capture
        list so a deferred TTS-done update for THIS turn won't be
        clobbered by the next turn's reset.

        Args:
            turn_start_perf: ``time.perf_counter()`` snapshot from the
                wrapper at turn entry. Callback computes elapsed delta.
            cell: One-element mutable list. Callback writes elapsed ms
                into ``cell[0]``. Wrapper / deferred update reads it.
        """
        # Stable closure capturing the cell — replaces any prior turn's
        # callback on the engine via Tier 1.
        def _on_first_chunk() -> None:
            # Single store under GIL — atomic per CPython.
            elapsed_ms = int((time.perf_counter() - turn_start_perf) * 1000)
            if elapsed_ms < 0:
                return  # clock skew guard
            cell[0] = elapsed_ms
            # Legacy mirror for non-deferred consumers (cloud streaming
            # path reads self._first_audio_at directly via _flush_trace).
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

    # REVIEW [B25] L1406-1453 ACTIVE · _defer_ttfs_update (async 本地 TTS 的 ttfs 后补)
    # REVIEW 分析: 给 _tts_future 挂 done_callback；audio thread 写完 ttfs_cell 后 patch trace row；handle fut already done race
    # REVIEW 建议: 留
    # REVIEW 评估: ___
    def _defer_ttfs_update(
        self,
        trace_id: int,
        ttfs_cell: list[int | None],
    ) -> None:
        """Schedule a ttfs_ms update once async TTS playback completes.

        Local-path turns (farewell / router-with-text)
        dispatch TTS via ``_speak_nonblocking`` which submits
        to a background executor and returns immediately, so _flush_trace
        runs BEFORE the first audio chunk plays. We hook a done-callback on
        the most-recent ``_tts_future``: when synthesis + playback finish,
        the per-turn ``ttfs_cell`` will have been written by the audio
        thread; we patch the trace row via ``trace_log.update_ttfs``.

        No-op when there's no pending future or the cell is already
        populated (cloud path captured it synchronously).

        Args:
            trace_id: Row id returned by ``_flush_trace``.
            ttfs_cell: Same one-element list passed to _arm_tts_first_chunk.
        """
        fut = getattr(self, "_tts_future", None)
        if fut is None:
            return

        def _on_tts_done(_fut: Future) -> None:
            ttfs = ttfs_cell[0]
            if ttfs is None or ttfs < 0 or ttfs > 60_000:
                return
            try:
                self.trace_log.update_ttfs(trace_id, ttfs)
            except Exception as exc:
                self.logger.debug("Deferred ttfs update failed: %s", exc)

        if fut.done():
            # Already finished but ttfs_cell empty — race: audio thread
            # may not yet have run. Schedule a fixed micro-delay check
            # via the executor so we don't block the wrapper.
            try:
                self._executor.submit(_on_tts_done, fut)
            except Exception:
                pass
            return
        try:
            fut.add_done_callback(_on_tts_done)
        except Exception as exc:
            self.logger.debug("Could not register ttfs done-callback: %s", exc)

    # REVIEW [B26] L1455-1782 MIXED · _flush_trace (trace v3 行组装)
    # REVIEW 分析: 327 行；session/turn/latency 计算→ttfs_ms 三级 fallback→input_metadata→tool_calls→4-way LLM source 分支：(a) cloud ACTIVE / (b) router-driven L1603-1632 DEAD (`_last_route` 永远 None) / (c) pure-local-router L1633-1654 DEAD (同上) / (d) regex ACTIVE→cost→cited_obs→empty→NULL→log_turn→async NLI lag-1→async Observer
    # REVIEW 建议: 删 (b)+(c) 整段 L1603-1654 (52 行)，逻辑改成 if cloud / elif regex / else 直接进 log_turn；与 B22 七字段删除联动
    # REVIEW 评估: ___
    def _flush_trace(
        self,
        *,
        text: str,
        session_id: str,
        user_id: str,
        emotion: str,
        turn_start: float,
        ttfs_ms_known: int | None = None,
    ) -> int | None:
        """Assemble a v3 trace row from staged _last_* state and write it.

        Always runs in the outer wrapper's finally — covers normal returns,
        early returns (farewell / etc.), and exceptions. Any
        failure here is logged and swallowed; trace logging must never
        bring down the conversation pipeline.

        Args:
            ttfs_ms_known: Per-turn ttfs_cell[0] value at flush time. None
                if the audio thread hasn't fired yet (typical for async
                local-path TTS — caller schedules a deferred update_ttfs).

        Returns:
            The new trace row id, or None on failure (so callers can skip
            the deferred ttfs_ms update).
        """
        try:
            from memory.cold.pricing import compute_cost_usd

            # Trace v3: session_id = per-launch app session, NOT user_id.
            # The conversation_store still keys history by user_id (passed
            # in as `session_id` arg here from legacy callers), but for trace
            # we want a launch-scoped session boundary so (session_id, turn_id)
            # is meaningful for analytics.
            trace_session = self._app_session_id
            session_turn = self._turn_counter.get(trace_session, 0) + 1
            self._turn_counter[trace_session] = session_turn

            # Total latency: prefer the inner-snapshot moment over now() so
            # late finally-block delays (slow SQLite / ThreadPool contention
            # / TTS pipeline draining) don't inflate latency_ms past the
            # actual end-of-turn perceived by the user.
            _end_basis = self._last_turn_end_at if self._last_turn_end_at else time.monotonic()
            total_ms = int((_end_basis - turn_start) * 1000)
            self._last_timings["total_ms"] = total_ms

            # ttfs_ms preference order:
            # 1. Per-turn ttfs_cell value (already an elapsed delta computed
            #    by the audio-thread callback against the same turn_start_perf
            #    — see _arm_tts_first_chunk).
            # 2. Legacy self._first_audio_at fallback for code paths that
            #    might not pass ttfs_ms_known.
            # 3. None — async TTS hasn't fired yet; deferred update_ttfs
            #    will patch the row when playback completes.
            ttfs_ms: int | None = ttfs_ms_known
            if ttfs_ms is None and self._first_audio_at is not None:
                _legacy = int((self._first_audio_at - turn_start) * 1000)
                if 0 <= _legacy < 60_000:
                    ttfs_ms = _legacy

            # input_metadata: stable shape; emit None for absent fields.
            # asr_ms also stashed here on voice path (None on text path).
            input_metadata = {
                "asr_text_raw": self._last_asr_text_raw,
                "asr_confidence": self._last_asr_confidence,
                "vad_duration_ms": self._last_vad_duration_ms,
                "audio_path": self._last_audio_path,
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
            # Two cases:
            # (a) Cloud path: main LLMClient (self.llm). Metadata harvested
            #     in cloud finally via self._last_llm_metadata + tokens.
            # (b) L0 regex fast-path or farewell: no LLM ran, all llm_*
            #     stay NULL; only regex_* identifiers are recorded.
            llm_metadata = self._last_llm_metadata
            llm_tokens_in: int | None = None
            llm_tokens_out: int | None = None
            cache_read = self._last_cache_read_tokens
            cache_write = llm_metadata.get("cache_creation_input_tokens") if llm_metadata else None
            llm_model_used: str | None = None
            finish_reason_used = self._last_finish_reason

            if self._last_path == "cloud":
                # (a) Main chat LLM. Tokens stashed in self._last_llm_tokens.
                llm_model_used = self.llm.model
                llm_tokens_in = self._last_llm_tokens.get("input") if self._last_llm_tokens else None
                llm_tokens_out = self._last_llm_tokens.get("output") if self._last_llm_tokens else None
            elif self._last_path == "regex" and self._last_regex_match is not None:
                # (b) L0 regex fast-path — no LLM, no tokens.
                rm = self._last_regex_match
                if llm_metadata is None:
                    llm_metadata = {}
                llm_metadata["regex_pattern_id"] = rm.pattern_id
                llm_metadata["regex_intent"] = rm.intent
                llm_metadata["regex_tool"] = rm.tool_name

            cost = compute_cost_usd(
                model=llm_model_used,
                tokens_in=llm_tokens_in,
                tokens_out=llm_tokens_out,
                cache_read_in=cache_read,
                cache_write_in=cache_write,
                pricing_table=self._pricing_table,
            )

            assistant_text = self._last_response_text or ""

            # Parse <cited_obs>[...] from LLM response, filter against actually-injected
            # observation ids (defense against hallucinated citations).
            parsed_cited = memory_trace.parse_cited_obs_ids(assistant_text)
            injected = set(self._last_memory_hits.injected_observation_ids) if self._last_memory_hits else set()
            cited_obs_ids = (
                [i for i in parsed_cited if i in injected]
                if parsed_cited is not None else None
            )

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
                cited_obs_ids=cited_obs_ids,
                prompt_version=self._prompt_version,
                latency_ms=total_ms,
                ttfs_ms=ttfs_ms,
                latency_breakdown={
                    "asr_ms":          self._last_asr_ms,
                    "route_ms":        self._last_timings.get("route_ms"),
                    "memory_query_ms": self._last_timings.get("memory_query_ms"),
                    "local_exec_ms":   self._last_timings.get("local_exec_ms"),
                    "llm_first_ms":    self._last_timings.get("llm_first_ms"),
                    "tts_first_ms":    ttfs_ms,
                    "total_ms":        total_ms,
                },
                end_reason=self._last_end_reason,
                error=self._last_error,
                finish_reason=finish_reason_used,
                cost_usd=cost,
                tts_chars_synthesized=self._last_tts_chars_synthesized or None,
            )

            # Async NLI outcome resolution for the PREVIOUS turn (lag-1 model).
            # The CURRENT turn's text ("好的") is used to judge the PREVIOUS
            # turn's outcome — same lag-1 semantics as the old sync regex path.
            # Submits to executor and returns immediately; does not block flush.
            if self._last_trace_id is not None:
                prev_id = self._last_trace_id
                # text is the current turn's user utterance — what the user said
                # in THIS turn that may signal approval/disapproval of the PRIOR
                # turn's LLM response.
                cur_text = text
                cur_trace_id = trace_id
                nli = self.nli_classifier
                tl = self.trace_log

                def _resolve_outcome() -> None:
                    from memory.cold.outcome_detector import detect_outcome
                    # Skip if thumbs already set (explicit user feedback > NLI).
                    existing = tl.query_by_trace_id(prev_id)
                    if existing and existing.get("outcome_signal") is not None:
                        return
                    signal = detect_outcome(cur_text, nli=nli)
                    if signal is not None:
                        tl.update_outcome(prev_id, signal=signal, at_turn_id=cur_trace_id)
                        LOGGER.debug(
                            "trace outcome updated: id=%d signal=%d", prev_id, signal
                        )

                self._executor.submit(_resolve_outcome)

            # Carry this turn's trace id forward for next turn's outcome resolution.
            self._last_trace_id = trace_id

            # Return trace_id BEFORE the async observer submission so
            # callers (the wrapper) can register a deferred ttfs update
            # without waiting for the observer to start.
            _trace_id_to_return = trace_id

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
            return _trace_id_to_return
        except Exception as exc:
            self.logger.warning("Trace flush failed (non-fatal): %s", exc)
            return None

    # REVIEW [B27] L1784-1819 ACTIVE · TTS 输出三件套 (speak / _speak_nonblocking / _wait_tts)
    # REVIEW 分析: speak blocking；_speak_nonblocking 提交到 executor + 完成 emit idle；_wait_tts block 直到上一轮完成 (timeout 30s)
    # REVIEW 建议: 留
    # REVIEW 评估: ___
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

    # REVIEW [B28] L1821-1825 ACTIVE · _on_voice_interrupt
    # REVIEW 分析: InterruptMonitor 关键词命中→ _cancel.set + _cancel_current
    # REVIEW 建议: 留
    # REVIEW 评估: ___
    def _on_voice_interrupt(self) -> None:
        """Called by InterruptMonitor when an interrupt keyword is detected."""
        self.logger.info("Voice interrupt detected")
        self._cancel.set()
        self._cancel_current()

    # REVIEW [B29] L1827-1869 ACTIVE · _truncate_assistant_for_interrupt (WP5 历史改写)
    # REVIEW 分析: 把最后 assistant 消息缩成已播部分 + "..."，append "[Interrupted by user]" 让 LLM 下轮知道用户实际听到啥；handle openai/anthropic 两种 content shape
    # REVIEW 建议: 留
    # REVIEW 评估: ___
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

    # REVIEW [B30] L1871-1887 MIXED · _on_voice_resume
    # REVIEW 分析: resume 关键词命中→_cancel.set + pipeline.abort + tts.stop；docstring (L1872-1876) 提"do NOT clear _interrupted_response — the resume check in _process_turn needs it"，但 resume read path 已删 (commit 4692e3d) → STALE-DOC
    # REVIEW 建议: 留 abort/stop 行为 (resume 关键词仍想 stop TTS)；删/改 L1872-1876 docstring；与 IB9 _interrupted_response 一并清
    # REVIEW 评估: ___
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

    # REVIEW [B31] L1889-1907 ACTIVE · WP7 soft-stop 对 (_on_soft_pause + _on_soft_resume)
    # REVIEW 分析: VAD-driven TTS ducking；user 开口→suspend；VAD 结束/无关键词超时→resume
    # REVIEW 建议: 留
    # REVIEW 评估: ___
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

    # REVIEW [B32] L1909-1937 MIXED · _cancel_current
    # REVIEW 分析: pipeline.abort→snapshot played_texts (WP5)→cancel _tts_future→tts.stop；DEAD 1: L1914 + L1923-1924 写 _interrupted_response (无 reader)；DEAD 2: L1932-1936 `hasattr(self,"skill_factory")` 永 False (整 repo 不在 JarvisApp 上 set)
    # REVIEW 建议: 删 L1914/L1923-1924 + L1932-1936 整个 try 块；保留 played_texts/abort/stop 主逻辑
    # REVIEW 评估: ___
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

    # REVIEW [B33] L1939-1948 ACTIVE · _flush_stdin (清 stdin buffer)
    # REVIEW 分析: 防止录音时多按 Enter 残留进下轮 input()
    # REVIEW 建议: 留
    # REVIEW 评估: ___
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

    # REVIEW [B34] L1950-1957 DEAD · JarvisApp.speak_short
    # REVIEW 分析: 全 repo 无 JarvisApp 这层 caller；TTSEngine.speak_short 是另一个东西，活着但不依赖此 wrapper
    # REVIEW 建议: 删整个方法
    # REVIEW 评估: ___
    def speak_short(self, text: str) -> None:
        """Speak a brief acknowledgment (low latency)."""
        tts = self._get_tts()
        if tts:
            try:
                tts.speak_short(text)
            except Exception as exc:
                self.logger.warning("TTS short failed: %s", exc)

    # REVIEW [B35] L1959-1969 ACTIVE · _get_tts (TTS 懒加载)
    # REVIEW 分析: 首次调用时构造 TTSEngine；失败返 None (graceful)
    # REVIEW 建议: 留
    # REVIEW 评估: ___
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

    # REVIEW [B36] L1971-1983 ACTIVE · _create_tts_pipeline (流式 TTS 工厂)
    # REVIEW 分析: cloud LLM 路径用；构造 TTSPipeline.start() 返回，失败返 None
    # REVIEW 建议: 留
    # REVIEW 评估: ___
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

    # REVIEW [B37] L1985-1994 ACTIVE · _warmup_interrupt_vad
    # REVIEW 分析: 启动时预热 InterruptMonitor 的 Silero VAD (lazy load 触发 + 喂 1 帧零样本)
    # REVIEW 建议: 留
    # REVIEW 评估: ___
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

    # REVIEW [B38] L1996-2012 ACTIVE · 用户身份 helpers (_resolve_display_name + _resolve_role)
    # REVIEW 分析: 两个 user_store 查询 wrapper；guest 默认；都很简单
    # REVIEW 建议: 留
    # REVIEW 评估: ___
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

    # REVIEW [B39] L2014-2028 ACTIVE · _print_banner (启动横幅)
    # REVIEW 分析: clear screen + print 设备模式/用户数/工具数/LLM model
    # REVIEW 建议: 留
    # REVIEW 评估: ___
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

    # REVIEW [B40] L2030-2081 ACTIVE · 早报对 (_setup_morning_briefing + _run_morning_briefing)
    # REVIEW 分析: 默认 7:00 cron；含 weather + reminders + todos 三段，按 config.scheduler.morning_briefing.include 选；speak() 播报
    # REVIEW 建议: 留
    # REVIEW 评估: ___
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


# REVIEW [B41] L2084-2088 ACTIVE · load_config
# REVIEW 分析: yaml.safe_load
# REVIEW 建议: 留
# REVIEW 评估: ___
def load_config(config_path: str | Path) -> dict[str, Any]:
    """Load YAML config from disk."""
    path = Path(config_path)
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# REVIEW [B42] L2091-2099 ACTIVE · deep_merge (config overlay)
# REVIEW 分析: 递归合并 overlay (deploy/pi.yaml) 到 base config
# REVIEW 建议: 留
# REVIEW 评估: ___
def deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge overlay into base. Overlay values win."""
    merged = dict(base)
    for key, value in overlay.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


# REVIEW [B43] L2102-2109 ACTIVE · configure_logging
# REVIEW 分析: 根 logger basicConfig
# REVIEW 建议: 留
# REVIEW 评估: ___
def configure_logging(config: dict) -> None:
    """Configure root logging."""
    level_name = str(config.get("logging", {}).get("level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


# REVIEW [B44] L2112-2158 ACTIVE · main (CLI 入口)
# REVIEW 分析: argparse (--no-wake/--config/--config-overlay) → load_config → deep_merge → JarvisApp(config) → run_interactive 或 run_always_listening
# REVIEW 建议: 留
# REVIEW 评估: ___
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


# REVIEW [B45] L2161-2162 ACTIVE · entry guard
# REVIEW 建议: 留
# REVIEW 评估: ___
if __name__ == "__main__":
    raise SystemExit(main())
