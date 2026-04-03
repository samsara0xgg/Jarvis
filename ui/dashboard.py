"""Gradio dashboard for Jarvis — minimal dark UI."""

from __future__ import annotations

import logging
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

import numpy as np
from scipy import signal

from core.command_parser import COLOR_XY_MAP
from jarvis import JarvisApp, configure_logging, load_config

LOGGER = logging.getLogger(__name__)

# Responses that indicate local executor couldn't handle it → fall through to LLM
_GARBAGE_PATTERNS = ("没查到", "未找到", "暂不支持", "无法", "不支持")


def _is_useful(text: str) -> bool:
    """Return False if the local response is a generic failure message."""
    if not text or not text.strip():
        return False
    return not any(p in text for p in _GARBAGE_PATTERNS)

# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------
CSS = """
.gradio-container { max-width: 880px !important; margin: auto; }
footer { display: none !important; }
"""


class DashboardController:
    """Lightweight controller — does ASR + LLM text only, no TTS."""

    def __init__(
        self,
        config_path: str | Path | None = None,
        *,
        app: JarvisApp | None = None,
    ) -> None:
        self.config_path = (
            Path(config_path) if config_path is not None
            else Path(__file__).resolve().parents[1] / "config.yaml"
        )
        if app is not None:
            self.app = app
            self.config = deepcopy(app.config)
        else:
            self.config = load_config(self.config_path)
            configure_logging(self.config)
            self.app = JarvisApp(self.config, config_path=self.config_path)

    # ------------------------------------------------------------------
    # Voice — lightweight pipeline (no TTS, no blocking)
    # ------------------------------------------------------------------

    def handle_voice(
        self, audio_input: tuple[int, np.ndarray] | list[Any] | None,
    ) -> str:
        """ASR + intent + LLM → text response. No TTS playback."""
        if audio_input is None:
            return ""

        audio = self._coerce_audio(audio_input)
        app = self.app

        # 1. ASR
        try:
            transcription = app.speech_recognizer.transcribe(np.copy(audio))
        except Exception as exc:
            LOGGER.exception("ASR failed")
            return f"语音识别失败: {exc}"

        text = transcription.text.strip()
        if not text:
            return "没听清，请再说一遍。"

        # 2. Speaker verification
        user_id = None
        user_role = "guest"
        user_name = None
        try:
            verification = app.speaker_verifier.verify(np.copy(audio))
            if verification.verified:
                user_id = verification.user
                user_name = app._resolve_display_name(user_id)
                user_role = app._resolve_role(user_id)
        except Exception:
            pass

        # 3. Keyword trigger
        response_text = None
        if app.rule_manager and app.local_executor:
            match = app.rule_manager.check_keyword(text)
            if match:
                actions, rule_name = match
                ar = app.local_executor.execute_smart_home(
                    actions, user_role, response=f"好的，{rule_name}已执行。",
                )
                response_text = ar.text

        # 4. Intent routing → local execution
        local_data = None  # For REQLLM rephrasing
        if response_text is None and app.intent_router and app.local_executor:
            from core.local_executor import Action
            route = app.intent_router.route(text)
            ar = None
            if route.tier == "local":
                if route.intent == "smart_home":
                    ar = app.local_executor.execute_smart_home(
                        route.actions, user_role, response=route.response,
                    )
                elif route.intent == "info_query":
                    ar = app.local_executor.execute_info_query(
                        route.sub_type, route.query, user_role,
                    )
                elif route.intent == "time":
                    ar = app.local_executor.execute_time(route.sub_type)
                elif route.intent == "automation":
                    ar = app.local_executor.execute_automation(
                        route.sub_type, route.rule,
                    )
                if ar is not None:
                    if ar.action == Action.REQLLM:
                        local_data = ar.text
                    elif _is_useful(ar.text):
                        response_text = ar.text
                    # else: garbage → fall through to LLM

        # 5. Cloud LLM fallback (non-streaming, no TTS)
        if response_text is None:
            try:
                session_id = user_id or "_guest"
                history = app.conversation_store.get_history(session_id)
                tools = app.skill_registry.get_tool_definitions(user_role)
                if local_data:
                    llm_input = (
                        f"用户问的是：{text}\n"
                        f"以下是查到的信息，用你自己的话简短转述给用户：\n{local_data}"
                    )
                else:
                    llm_input = text
                response_text, msgs = app.llm.chat(
                    user_message=llm_input,
                    conversation_history=history,
                    tools=tools,
                    tool_executor=app.skill_registry.execute,
                    user_name=user_name,
                    user_id=user_id,
                    user_role=user_role,
                )
                app.conversation_store.replace(session_id, msgs)
            except Exception as exc:
                LOGGER.exception("LLM failed")
                response_text = f"云端服务不可用: {exc}"

        prefix = f"🎤 {user_name or 'Guest'}: {text}\n\n" if text else ""
        return f"{prefix}🤖 {response_text}"

    # ------------------------------------------------------------------
    # Scenes
    # ------------------------------------------------------------------

    def trigger_scene(self, name: str) -> str:
        engine = getattr(self.app, "automation_engine", None)
        if not engine:
            return "自动化引擎未启用。"
        try:
            results = engine.execute_scene(name)
            return "；".join(results) if results else f"'{name}' 已执行。"
        except Exception as exc:
            return f"场景失败: {exc}"

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render_header(self) -> str:
        mode = self.config.get("devices", {}).get("mode", "sim")
        devices = len(self.app.device_manager.get_all_status())
        skills = len(self.app.skill_registry.skill_names)
        return (
            '<div style="display:flex;gap:10px;align-items:center;padding:10px 14px;'
            'background:#141414;border-radius:12px;color:#aaa;font-size:13px;">'
            '<b style="color:#e0e0e0;font-size:16px;">小贾</b>'
            f'<span style="padding:2px 8px;border-radius:99px;background:#1e3a1e;color:#6fcf6f;font-size:12px;">{mode}</span>'
            f'<span>设备 {devices}</span><span>技能 {skills}</span>'
            '</div>'
        )

    def render_devices(self) -> str:
        statuses = self.app.device_manager.get_all_status()
        if not statuses:
            return '<div style="color:#555;padding:8px;">暂无设备</div>'
        cards = []
        for did, s in statuses.items():
            swatch = self._swatch(s)
            name = s.get("name", did)
            on = s.get("is_on")
            state_label = ""
            if s.get("device_type") == "door_lock":
                state_label = "🔒" if s.get("is_locked", True) else "🔓"
            elif on is True:
                state_label = "开"
            elif on is False:
                state_label = "关"
            cards.append(
                f'<div style="display:inline-flex;align-items:center;gap:8px;'
                f'padding:8px 12px;background:#1a1a1a;border:1px solid #2a2a2a;'
                f'border-radius:10px;font-size:13px;color:#ccc;">'
                f'<span style="width:10px;height:10px;border-radius:50%;background:{swatch};"></span>'
                f'{name} <span style="color:#666;">{state_label}</span>'
                f'</div>'
            )
        return '<div style="display:flex;flex-wrap:wrap;gap:6px;">' + "".join(cards) + '</div>'

    def render_health(self) -> str:
        tracker = getattr(self.app, "health_tracker", None)
        if not tracker:
            return '<span style="color:#555;">未启用</span>'
        statuses = tracker.get_all_statuses()
        if not statuses:
            return '<span style="color:#555;">暂无数据</span>'
        colors = {"healthy": "#2e7d32", "degraded": "#e65100", "unavailable": "#c62828"}
        labels = {"healthy": "正常", "degraded": "降级", "unavailable": "不可用"}
        chips = []
        for name, st in sorted(statuses.items()):
            c = colors.get(st.status.value, "#555")
            l = labels.get(st.status.value, "?")
            chips.append(
                f'<span style="display:inline-block;padding:4px 10px;margin:2px;'
                f'border-radius:8px;background:{c};color:white;font-size:12px;">'
                f'{name}: {l}</span>'
            )
        return "".join(chips)

    def render_memory(self) -> str:
        """Return a plain-text summary of memory stats per user."""
        try:
            store = self.app.memory_manager.store
            user_ids = store.get_all_user_ids()
            lines = []
            for uid in user_ids:
                count = store.count_active(uid)
                episodes = store.get_recent_episodes(uid, days=3)
                ep_count = len(episodes)
                lines.append(f"{uid}: {count} 条记忆, {ep_count} 条近期经历")
            return "\n".join(lines) if lines else "暂无记忆数据"
        except Exception:
            return "记忆系统未就绪"

    def refresh(self) -> tuple[str, str, str, str]:
        return self.render_header(), self.render_devices(), self.render_health(), self.render_memory()

    # ------------------------------------------------------------------
    # Audio
    # ------------------------------------------------------------------

    def _coerce_audio(self, audio_input: tuple[int, np.ndarray] | list[Any]) -> np.ndarray:
        if not isinstance(audio_input, (tuple, list)) or len(audio_input) != 2:
            raise ValueError("Invalid audio.")
        sr = int(audio_input[0])
        data = np.asarray(audio_input[1])
        if data.ndim > 1:
            data = np.mean(data, axis=-1)
        if np.issubdtype(data.dtype, np.integer):
            info = np.iinfo(data.dtype)
            data = data.astype(np.float32) / float(max(abs(info.min), info.max))
        else:
            data = data.astype(np.float32, copy=False)
        target = int(self.config.get("audio", {}).get("sample_rate", 16000))
        if sr != target:
            gcd = int(np.gcd(sr, target))
            data = signal.resample_poly(data, target // gcd, sr // gcd).astype(np.float32)
        return np.clip(data, -1.0, 1.0)

    def _swatch(self, status: dict[str, Any]) -> str:
        if not status.get("is_available", True):
            return "#444"
        xy = status.get("color_xy")
        if isinstance(xy, (list, tuple)) and len(xy) == 2:
            return _xy_hex(float(xy[0]), float(xy[1]))
        cn = str(status.get("color", "")).strip().lower()
        if cn and cn in COLOR_XY_MAP:
            xy = COLOR_XY_MAP[cn]
            return _xy_hex(float(xy[0]), float(xy[1]))
        if status.get("device_type") == "door_lock":
            return "#ef4444" if status.get("is_locked", True) else "#22c55e"
        if status.get("device_type") == "thermostat":
            return "#f97316"
        if status.get("is_on") is True:
            return "#facc15"
        return "#333"


def _xy_hex(x: float, y: float) -> str:
    if y <= 0:
        return "#333"
    z = max(0.0, 1.0 - x - y)
    Y = 1.0
    X, Z = (Y / y) * x, (Y / y) * z
    r = X * 1.656492 - Y * 0.354851 - Z * 0.255038
    g = -X * 0.707196 + Y * 1.655397 + Z * 0.036152
    b = X * 0.051713 - Y * 0.121364 + Z * 1.01153
    rgb = [max(0.0, c) for c in (r, g, b)]
    mx = max(rgb)
    if mx > 0:
        rgb = [c / mx for c in rgb]
    rgb = [12.92 * v if v <= 0.0031308 else 1.055 * pow(v, 1 / 2.4) - 0.055 for v in rgb]
    return "#%02x%02x%02x" % tuple(int(max(0.0, min(1.0, c)) * 255) for c in rgb)


# ======================================================================
# Gradio UI
# ======================================================================

def build_dashboard(
    config_path: str | Path | None = None,
    *,
    controller: DashboardController | None = None,
):
    try:
        import gradio as gr
    except ImportError as exc:
        raise RuntimeError("pip install gradio") from exc

    ctrl = controller or DashboardController(config_path)
    scenes = list(ctrl.config.get("automations", {}).keys())

    with gr.Blocks(title="小贾 Jarvis", css=CSS, theme=gr.themes.Default()) as demo:

        header = gr.HTML(value=ctrl.render_header())

        with gr.Row():
            # Left: devices + scenes
            with gr.Column(scale=4):
                gr.Markdown("**设备**")
                devices = gr.HTML(value=ctrl.render_devices())
                if scenes:
                    gr.Markdown("**场景**")
                    with gr.Row():
                        scene_btns = [gr.Button(n, size="sm") for n in scenes]
                gr.Markdown("**健康**")
                health = gr.HTML(value=ctrl.render_health())
                gr.Markdown("**记忆**")
                memory = gr.Textbox(
                    value=ctrl.render_memory(),
                    label="",
                    lines=4,
                    interactive=False,
                )

            # Right: voice
            with gr.Column(scale=6):
                gr.Markdown("**对话**")
                mic = gr.Audio(sources=["microphone"], type="numpy", label="录音")
                response = gr.Textbox(
                    label="回复",
                    lines=6,
                    interactive=False,
                    placeholder="录音后显示回复…",
                )

        # ── Events ──
        if hasattr(mic, "stop_recording"):
            mic.stop_recording(fn=ctrl.handle_voice, inputs=[mic], outputs=[response])
        else:
            mic.change(fn=ctrl.handle_voice, inputs=[mic], outputs=[response])

        if scenes:
            for name, btn in zip(scenes, scene_btns):
                btn.click(fn=lambda n=name: ctrl.trigger_scene(n), outputs=[response])

        gr.Timer(5.0).tick(fn=ctrl.refresh, outputs=[header, devices, health, memory])

    return demo


def launch_dashboard(config_path: str | Path | None = None, **kwargs: Any) -> None:
    build_dashboard(config_path).launch(**kwargs)


if __name__ == "__main__":
    launch_dashboard()
