"""Gradio Blocks dashboard for the smart-home voice lock application."""

from __future__ import annotations

from copy import deepcopy
import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
from scipy import signal

from auth.enrollment import EnrollmentService
from core.command_parser import COLOR_XY_MAP
from devices.hue.hue_bridge import HueBridgeAuthenticationError, HueBridgeConnectionError
from main import PipelineResult, SmartHomeVoiceLockApp, configure_logging, load_config

LOGGER = logging.getLogger(__name__)
ASCII_LAYOUT = """```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ 顶部状态栏: 当前模式 | Bridge 连接状态 | 已注册用户数 | 设备数                │
├───────────────────────────────┬──────────────────────────────────────────────┤
│ 左侧设备面板                  │ 语音控制区                                   │
│ - 实时设备状态                │ - 按住说话 / 麦克风录音                      │
│ - live 模式显示真实灯光颜色   │ - Pipeline 结果展示                          │
│                               ├──────────────────────────────────────────────┤
│                               │ 快捷场景面板                                 │
│                               │ - 场景按钮                                   │
├───────────────────────────────┴──────────────────────────────────────────────┤
│ Tabs: 用户管理 | 操作日志 | Hue 设置                                         │
└──────────────────────────────────────────────────────────────────────────────┘
```"""

USER_TABLE_HEADERS = ["user_id", "name", "role", "permissions", "enrolled_at"]
LOG_TAIL_LINES = 50


class _BufferedAudioRecorder:
    """Recorder adapter that replays browser-provided audio samples."""

    def __init__(
        self,
        samples: list[np.ndarray],
        quality_checker: Any,
    ) -> None:
        """Store pre-recorded audio samples for enrollment replay."""

        self._samples = list(samples)
        self._quality_checker = quality_checker

    def record(self, duration: float | None = None) -> np.ndarray:
        """Return the next buffered sample."""

        del duration
        if not self._samples:
            raise RuntimeError("No buffered enrollment audio samples remaining.")
        return self._samples.pop(0)

    def is_quality_ok(self, audio: np.ndarray) -> tuple[bool, str]:
        """Delegate audio quality validation to the existing recorder."""

        return self._quality_checker.is_quality_ok(audio)


class DashboardController:
    """Controller layer that adapts the existing pipeline for the web dashboard."""

    def __init__(
        self,
        config_path: str | Path | None = None,
        *,
        app: SmartHomeVoiceLockApp | None = None,
    ) -> None:
        """Initialize the dashboard controller.

        Args:
            config_path: Optional path to `config.yaml`.
            app: Optional injected application instance for tests.
        """

        self.config_path = (
            Path(config_path)
            if config_path is not None
            else Path(__file__).resolve().parents[1] / "config.yaml"
        )
        self.logger = LOGGER

        if app is not None:
            self.app = app
            self.config = deepcopy(app.config)
        else:
            self.config = load_config(self.config_path)
            configure_logging(self.config)
            self.app = SmartHomeVoiceLockApp(self.config, config_path=self.config_path)

        self.log_path = self._resolve_log_path()

    def refresh_overview(self) -> tuple[str, str, list[list[str]], str, str]:
        """Return the latest dashboard snapshot.

        Returns:
            Header HTML, device panel HTML, user rows, log text, and Hue status.
        """

        return (
            self.render_header_html(),
            self.render_device_panel_html(),
            self.get_user_rows(),
            self.get_operation_logs(),
            self.render_hue_status_markdown(),
        )

    def handle_voice_input(
        self,
        audio_input: tuple[int, np.ndarray] | list[Any] | None,
    ) -> tuple[str, str, str, str]:
        """Process microphone audio from the dashboard.

        Args:
            audio_input: Gradio audio payload `(sample_rate, waveform)`.

        Returns:
            Pipeline HTML, device panel HTML, header HTML, and logs text.
        """

        if audio_input is None:
            return (
                self.render_message_panel("等待录音输入。"),
                self.render_device_panel_html(),
                self.render_header_html(),
                self.get_operation_logs(),
            )

        audio = self._coerce_audio_input(audio_input)
        result = self.app.process_audio(audio)
        return (
            self.render_pipeline_html(result),
            self.render_device_panel_html(),
            self.render_header_html(),
            self.get_operation_logs(),
        )

    def trigger_scene(
        self,
        scene_name: str,
    ) -> tuple[str, str, str, str]:
        """Trigger a configured scene button from the dashboard.

        Args:
            scene_name: Canonical scene name from config.

        Returns:
            Pipeline/message HTML, device panel HTML, header HTML, and logs text.
        """

        try:
            scene_device = self.app.device_manager.get_device("scene")
        except KeyError:
            message = "当前模式未启用 Hue 场景。"
            return (
                self.render_message_panel(message),
                self.render_device_panel_html(),
                self.render_header_html(),
                self.get_operation_logs(),
            )

        try:
            execution_result = scene_device.execute("activate", scene_name)
        except Exception as exc:
            self.logger.exception("Scene trigger failed.")
            execution_result = f"场景触发失败: {exc}"

        return (
            self.render_message_panel(execution_result),
            self.render_device_panel_html(),
            self.render_header_html(),
            self.get_operation_logs(),
        )

    def register_user_from_samples(
        self,
        user_id: str,
        name: str,
        role: str,
        permissions_text: str,
        sample_1: tuple[int, np.ndarray] | list[Any] | None,
        sample_2: tuple[int, np.ndarray] | list[Any] | None,
        sample_3: tuple[int, np.ndarray] | list[Any] | None,
    ) -> tuple[str, list[list[str]], str, str]:
        """Register a user from three browser-recorded samples.

        Args:
            user_id: User identifier.
            name: User display name.
            role: Requested role.
            permissions_text: Comma-separated permissions string.
            sample_1: First enrollment sample.
            sample_2: Second enrollment sample.
            sample_3: Third enrollment sample.

        Returns:
            Registration message, updated user rows, header HTML, and logs text.
        """

        samples = [sample_1, sample_2, sample_3]
        if not user_id.strip() or not name.strip():
            message = "注册失败: user_id 和姓名不能为空。"
            return message, self.get_user_rows(), self.render_header_html(), self.get_operation_logs()
        if any(sample is None for sample in samples):
            message = "注册失败: 请提供 3 段录音样本。"
            return message, self.get_user_rows(), self.render_header_html(), self.get_operation_logs()

        processed_samples = [self._coerce_audio_input(sample) for sample in samples if sample is not None]
        permissions = [item.strip() for item in permissions_text.split(",") if item.strip()] or None
        buffered_recorder = _BufferedAudioRecorder(processed_samples, self.app.audio_recorder)
        enrollment_service = EnrollmentService(
            self.config,
            buffered_recorder,
            self.app.speaker_encoder,
            self.app.user_store,
        )

        try:
            user_record = enrollment_service.enroll_user(
                user_id=user_id,
                name=name,
                role=role.strip() or None,
                permissions=permissions,
            )
            message = f"注册成功：{user_record['name']} ({user_record['user_id']})"
        except Exception as exc:
            self.logger.exception("Dashboard enrollment failed.")
            message = f"注册失败: {exc}"

        return message, self.get_user_rows(), self.render_header_html(), self.get_operation_logs()

    def save_hue_settings(
        self,
        ip: str,
        username: str,
        auto_discover: bool,
        verify_ssl: bool,
        allow_http_fallback: bool,
        timeout_seconds: float,
    ) -> tuple[str, str, str]:
        """Persist Hue bridge settings to `config.yaml` and reload the app.

        Args:
            ip: Hue Bridge IP.
            username: Hue API username.
            auto_discover: Whether bridge auto-discovery is enabled.
            verify_ssl: Whether HTTPS certificates should be verified.
            allow_http_fallback: Whether HTTP fallback is allowed.
            timeout_seconds: Request timeout.

        Returns:
            Save message, header HTML, and Hue status markdown.
        """

        original_text = self._read_original_config_text()
        previous_app = self.app
        previous_config = deepcopy(self.config)
        updated_config = deepcopy(self.config)
        hue_bridge_config = updated_config.setdefault("hue", {}).setdefault("bridge", {})
        hue_bridge_config["ip"] = ip.strip()
        hue_bridge_config["username"] = username.strip()
        hue_bridge_config["auto_discover"] = bool(auto_discover)
        hue_bridge_config["verify_ssl"] = bool(verify_ssl)
        hue_bridge_config["allow_http_fallback"] = bool(allow_http_fallback)
        hue_bridge_config["timeout_seconds"] = float(timeout_seconds)

        self._write_config(updated_config)
        try:
            self.config = updated_config
            self.app = SmartHomeVoiceLockApp(self.config, config_path=self.config_path)
            self.log_path = self._resolve_log_path()
            message = "Hue 设置已保存。"
        except Exception as exc:
            self.logger.exception("Failed to reload app after saving Hue settings.")
            self.config_path.write_text(original_text, encoding="utf-8")
            self.config = previous_config
            self.app = previous_app
            self.log_path = self._resolve_log_path()
            message = f"Hue 设置保存失败，已回滚: {exc}"

        return message, self.render_header_html(), self.render_hue_status_markdown()

    def render_header_html(self) -> str:
        """Render the top status bar HTML."""

        mode = str(self.config.get("devices", {}).get("mode", "sim")).lower()
        bridge_status_label, bridge_status_class = self._get_bridge_status()
        user_count = len(self.app.user_store.get_all_users())
        device_count = len(self.app.device_manager.get_all_status())
        return f"""
<div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:12px 16px;
background:linear-gradient(135deg,#132a13,#31572c);border-radius:16px;color:#f7fee7;">
  <span style="font-weight:700;">smart-home-voice-lock</span>
  <span style="padding:4px 10px;border-radius:999px;background:#ecf39e;color:#132a13;">模式: {mode}</span>
  <span style="padding:4px 10px;border-radius:999px;background:{bridge_status_class};color:#fff;">Bridge: {bridge_status_label}</span>
  <span>用户数: {user_count}</span>
  <span>设备数: {device_count}</span>
</div>
"""

    def render_device_panel_html(self) -> str:
        """Render the left-side device panel as HTML cards."""

        statuses = self.app.device_manager.get_all_status()
        cards = [self._render_device_card(device_id, status) for device_id, status in statuses.items()]
        if not cards:
            cards.append("<div style='padding:16px;border:1px dashed #ccc;border-radius:12px;'>暂无设备</div>")
        return "<div style='display:flex;flex-direction:column;gap:12px;'>" + "".join(cards) + "</div>"

    def render_pipeline_html(self, result: PipelineResult) -> str:
        """Render the pipeline result block shown in the voice control panel."""

        verification = self.app._format_verification_line(result.verification_result, result.user_display_name)
        asr_text = result.transcription_result.text if result.transcription_result else ""
        parsed_command = self.app._format_parsed_command(result.parsed_command)
        permission = self.app._format_permission_line(result.permission_granted)
        return f"""
<div style="background:#101418;color:#f8fafc;padding:16px;border-radius:16px;font-family:ui-monospace,monospace;white-space:pre-wrap;">
┌ 声纹验证: {verification}
│ ASR 文本: {json.dumps(asr_text, ensure_ascii=False)}
│ 指令解析: {parsed_command}
│ 权限检查: {permission}
└ 执行结果: {result.execution_result}
</div>
"""

    def render_message_panel(self, message: str) -> str:
        """Render a generic status message panel."""

        return f"""
<div style="background:#1f2937;color:#f9fafb;padding:16px;border-radius:16px;">
{message}
</div>
"""

    def render_hue_status_markdown(self) -> str:
        """Render Markdown summary for the Hue settings tab."""

        mode = str(self.config.get("devices", {}).get("mode", "sim")).lower()
        bridge_config = self.config.get("hue", {}).get("bridge", {})
        ip = bridge_config.get("ip", "")
        username = bridge_config.get("username", "")
        bridge_status, _ = self._get_bridge_status()
        return (
            f"**当前模式**: `{mode}`\n\n"
            f"**Bridge 状态**: `{bridge_status}`\n\n"
            f"**Bridge IP**: `{ip or '未配置'}`\n\n"
            f"**Username**: `{username or '未配置'}`"
        )

    def get_operation_logs(self, tail_lines: int = LOG_TAIL_LINES) -> str:
        """Return the latest operation log lines."""

        if not self.log_path.exists():
            return "暂无操作日志。"

        lines = self.log_path.read_text(encoding="utf-8").splitlines()
        return "\n".join(lines[-tail_lines:]) or "暂无操作日志。"

    def get_user_rows(self) -> list[list[str]]:
        """Return user records formatted for a Gradio Dataframe."""

        rows: list[list[str]] = []
        for user in self.app.user_store.get_all_users():
            rows.append(
                [
                    str(user.get("user_id", "")),
                    str(user.get("name", "")),
                    str(user.get("role", "")),
                    ", ".join(str(permission) for permission in user.get("permissions", [])),
                    str(user.get("enrolled_at", "")),
                ]
            )
        return rows

    def get_hue_setting_values(self) -> tuple[str, str, bool, bool, bool, float]:
        """Return current Hue setting field values for the form."""

        bridge_config = self.config.get("hue", {}).get("bridge", {})
        return (
            str(bridge_config.get("ip", "")),
            str(bridge_config.get("username", "")),
            bool(bridge_config.get("auto_discover", True)),
            bool(bridge_config.get("verify_ssl", False)),
            bool(bridge_config.get("allow_http_fallback", True)),
            float(bridge_config.get("timeout_seconds", 5.0)),
        )

    def _coerce_audio_input(
        self,
        audio_input: tuple[int, np.ndarray] | list[Any],
    ) -> np.ndarray:
        """Convert Gradio audio input into a mono float32 waveform at 16 kHz."""

        if not isinstance(audio_input, (tuple, list)) or len(audio_input) != 2:
            raise ValueError("Invalid audio payload from Gradio.")

        sample_rate = int(audio_input[0])
        data = np.asarray(audio_input[1])
        if data.ndim > 1:
            data = np.mean(data, axis=-1)

        if np.issubdtype(data.dtype, np.integer):
            info = np.iinfo(data.dtype)
            scale = float(max(abs(info.min), info.max))
            waveform = data.astype(np.float32) / scale
        else:
            waveform = data.astype(np.float32, copy=False)

        target_sample_rate = int(self.config.get("audio", {}).get("sample_rate", 16000))
        if sample_rate != target_sample_rate:
            greatest_common_divisor = np.gcd(sample_rate, target_sample_rate)
            upsample = target_sample_rate // int(greatest_common_divisor)
            downsample = sample_rate // int(greatest_common_divisor)
            waveform = signal.resample_poly(waveform, upsample, downsample).astype(np.float32)

        return np.clip(waveform, -1.0, 1.0)

    def _render_device_card(self, device_id: str, status: dict[str, Any]) -> str:
        """Render a single device status card."""

        swatch = self._resolve_device_swatch(status)
        details = []
        for key, value in status.items():
            if key in {"device_id", "name", "device_type", "required_role", "color_xy", "color_temp_map"}:
                continue
            details.append(f"<div><strong>{key}</strong>: {value}</div>")

        return f"""
<div style="padding:14px 16px;border-radius:16px;background:#ffffff;border:1px solid #d9e2ec;box-shadow:0 6px 18px rgba(15,23,42,0.06);">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
    <div>
      <div style="font-weight:700;color:#102a43;">{status.get('name', device_id)}</div>
      <div style="font-size:12px;color:#486581;">{device_id} · {status.get('device_type', 'unknown')}</div>
    </div>
    <div style="width:18px;height:18px;border-radius:50%;border:2px solid #102a43;background:{swatch};"></div>
  </div>
  <div style="display:grid;gap:4px;font-size:13px;color:#243b53;">
    {''.join(details)}
  </div>
</div>
"""

    def _resolve_device_swatch(self, status: dict[str, Any]) -> str:
        """Resolve a status payload into a CSS color swatch."""

        if not status.get("is_available", True):
            return "#9aa5b1"

        color_xy = status.get("color_xy")
        if isinstance(color_xy, (list, tuple)) and len(color_xy) == 2:
            return self._xy_to_hex(float(color_xy[0]), float(color_xy[1]))

        color_name = str(status.get("color", "")).strip().lower()
        if color_name and color_name in COLOR_XY_MAP:
            xy = COLOR_XY_MAP[color_name]
            return self._xy_to_hex(float(xy[0]), float(xy[1]))

        if status.get("device_type") == "door_lock":
            return "#ef4444" if status.get("is_locked", True) else "#22c55e"
        if status.get("device_type") == "thermostat":
            return "#f97316"
        if status.get("is_on") is True:
            return "#facc15"
        return "#cbd2d9"

    def _xy_to_hex(self, x: float, y: float) -> str:
        """Approximate CIE xy coordinates as a display RGB color."""

        if y <= 0:
            return "#cbd2d9"

        z = max(0.0, 1.0 - x - y)
        Y = 1.0
        X = (Y / y) * x
        Z = (Y / y) * z

        r = X * 1.656492 - Y * 0.354851 - Z * 0.255038
        g = -X * 0.707196 + Y * 1.655397 + Z * 0.036152
        b = X * 0.051713 - Y * 0.121364 + Z * 1.01153

        rgb = [max(0.0, channel) for channel in (r, g, b)]
        if max(rgb) > 0:
            rgb = [channel / max(rgb) for channel in rgb]

        rgb = [self._gamma_correct(channel) for channel in rgb]
        return "#%02x%02x%02x" % tuple(int(max(0.0, min(1.0, channel)) * 255) for channel in rgb)

    def _gamma_correct(self, value: float) -> float:
        """Apply sRGB gamma correction."""

        if value <= 0.0031308:
            return 12.92 * value
        return 1.055 * pow(value, 1 / 2.4) - 0.055

    def _get_bridge_status(self) -> tuple[str, str]:
        """Return a human-readable bridge status label and badge color."""

        mode = str(self.config.get("devices", {}).get("mode", "sim")).lower()
        if mode != "live":
            return "SIM", "#4c956c"

        bridge = getattr(self.app.device_manager, "_hue_bridge", None)
        if bridge is None:
            return "未初始化", "#d97706"

        try:
            if bridge.is_connected():
                return "已连接", "#2f855a"
        except (HueBridgeConnectionError, HueBridgeAuthenticationError):
            pass
        return "未连接", "#c53030"

    def _resolve_log_path(self) -> Path:
        """Resolve the active audit log path from the running application when possible."""

        audit_logger = getattr(self.app, "audit_logger", None)
        if audit_logger is not None:
            for handler in getattr(audit_logger, "handlers", []):
                filename = getattr(handler, "baseFilename", None)
                if filename:
                    return Path(str(filename))
        return self.config_path.parent / "logs" / "operations.log"

    def _read_original_config_text(self) -> str:
        """Return the on-disk config contents or serialize the current config as fallback."""

        if self.config_path.exists():
            return self.config_path.read_text(encoding="utf-8")

        import yaml

        return yaml.safe_dump(self.config, allow_unicode=True, sort_keys=False)

    def _write_config(self, config: dict[str, Any]) -> None:
        """Write YAML config to disk."""

        import yaml

        with self.config_path.open("w", encoding="utf-8") as file_obj:
            yaml.safe_dump(config, file_obj, allow_unicode=True, sort_keys=False)


def build_dashboard(
    config_path: str | Path | None = None,
    *,
    controller: DashboardController | None = None,
):
    """Build the Gradio Blocks dashboard.

    Args:
        config_path: Optional path to `config.yaml`.
        controller: Optional injected controller for tests or embedding.

    Returns:
        A configured `gr.Blocks` instance.
    """

    try:
        import gradio as gr
    except ImportError as exc:
        raise RuntimeError("Gradio is not installed. Please install gradio to launch the dashboard.") from exc

    dashboard = controller or DashboardController(config_path)
    header_html, device_html, user_rows, log_text, hue_status = dashboard.refresh_overview()
    scene_names = list(dashboard.config.get("hue", {}).get("scene_aliases", {}).keys())
    hue_ip, hue_username, hue_auto_discover, hue_verify_ssl, hue_http_fallback, hue_timeout = dashboard.get_hue_setting_values()

    with gr.Blocks(title="smart-home-voice-lock Dashboard", theme=gr.themes.Soft()) as demo:
        gr.Markdown(ASCII_LAYOUT)
        header_bar = gr.HTML(value=header_html)

        with gr.Row(equal_height=False):
            with gr.Column(scale=5):
                gr.Markdown("### 设备面板")
                device_panel = gr.HTML(value=device_html)
            with gr.Column(scale=6):
                gr.Markdown("### 语音控制")
                mic_input = gr.Audio(
                    label="按住说话",
                    sources=["microphone"],
                    type="numpy",
                    waveform_options=gr.WaveformOptions(show_controls=False),
                )
                pipeline_panel = gr.HTML(
                    value=dashboard.render_message_panel("等待录音输入。")
                )

                gr.Markdown("### 快捷场景")
                with gr.Row():
                    if scene_names:
                        scene_buttons = [gr.Button(scene_name, variant="secondary") for scene_name in scene_names]
                    else:
                        scene_buttons = []
                        gr.Markdown("当前没有配置快捷场景。")

        with gr.Tabs():
            with gr.Tab("用户管理"):
                users_table = gr.Dataframe(
                    headers=USER_TABLE_HEADERS,
                    value=user_rows,
                    interactive=False,
                    label="已注册用户",
                )
                enrollment_status = gr.Markdown("填写信息并上传 3 段录音样本后完成注册。")
                with gr.Row():
                    enroll_user_id = gr.Textbox(label="user_id")
                    enroll_name = gr.Textbox(label="姓名")
                with gr.Row():
                    enroll_role = gr.Dropdown(
                        choices=["guest", "member", "resident", "admin", "owner"],
                        value="resident",
                        label="角色",
                    )
                    enroll_permissions = gr.Textbox(label="权限（逗号分隔）", placeholder="unlock,lights")
                with gr.Row():
                    enroll_audio_1 = gr.Audio(label="样本 1", sources=["microphone", "upload"], type="numpy")
                    enroll_audio_2 = gr.Audio(label="样本 2", sources=["microphone", "upload"], type="numpy")
                    enroll_audio_3 = gr.Audio(label="样本 3", sources=["microphone", "upload"], type="numpy")
                enroll_button = gr.Button("注册新用户", variant="primary")

            with gr.Tab("操作日志"):
                logs_box = gr.Textbox(
                    value=log_text,
                    lines=20,
                    interactive=False,
                    label="操作日志",
                )
                refresh_logs_button = gr.Button("刷新日志")

            with gr.Tab("Hue 设置"):
                hue_status_markdown = gr.Markdown(hue_status)
                with gr.Row():
                    hue_ip_input = gr.Textbox(value=hue_ip, label="Bridge IP")
                    hue_username_input = gr.Textbox(value=hue_username, label="Username")
                with gr.Row():
                    hue_auto_discover_input = gr.Checkbox(value=hue_auto_discover, label="自动发现 Bridge")
                    hue_verify_ssl_input = gr.Checkbox(value=hue_verify_ssl, label="校验 HTTPS 证书")
                    hue_http_fallback_input = gr.Checkbox(value=hue_http_fallback, label="允许 HTTP 回退")
                hue_timeout_input = gr.Number(value=hue_timeout, label="请求超时（秒）", precision=1)
                hue_save_message = gr.Markdown("修改 Hue 连接参数后点击保存。")
                hue_save_button = gr.Button("保存 Hue 设置")

        refresh_timer = gr.Timer(5.0)

        audio_handler_outputs = [pipeline_panel, device_panel, header_bar, logs_box]
        if hasattr(mic_input, "stop_recording"):
            mic_input.stop_recording(
                fn=dashboard.handle_voice_input,
                inputs=[mic_input],
                outputs=audio_handler_outputs,
            )
        else:
            mic_input.change(
                fn=dashboard.handle_voice_input,
                inputs=[mic_input],
                outputs=audio_handler_outputs,
            )

        for scene_name, scene_button in zip(scene_names, scene_buttons):
            scene_button.click(
                fn=lambda selected_scene=scene_name: dashboard.trigger_scene(selected_scene),
                inputs=None,
                outputs=audio_handler_outputs,
            )

        enroll_button.click(
            fn=dashboard.register_user_from_samples,
            inputs=[
                enroll_user_id,
                enroll_name,
                enroll_role,
                enroll_permissions,
                enroll_audio_1,
                enroll_audio_2,
                enroll_audio_3,
            ],
            outputs=[enrollment_status, users_table, header_bar, logs_box],
        )

        refresh_logs_button.click(
            fn=dashboard.get_operation_logs,
            inputs=None,
            outputs=[logs_box],
        )

        hue_save_button.click(
            fn=dashboard.save_hue_settings,
            inputs=[
                hue_ip_input,
                hue_username_input,
                hue_auto_discover_input,
                hue_verify_ssl_input,
                hue_http_fallback_input,
                hue_timeout_input,
            ],
            outputs=[hue_save_message, header_bar, hue_status_markdown],
        )

        refresh_timer.tick(
            fn=dashboard.refresh_overview,
            inputs=None,
            outputs=[header_bar, device_panel, users_table, logs_box, hue_status_markdown],
        )

    return demo


def launch_dashboard(
    config_path: str | Path | None = None,
    **launch_kwargs: Any,
) -> None:
    """Launch the Gradio dashboard.

    Args:
        config_path: Optional path to `config.yaml`.
        **launch_kwargs: Additional keyword arguments forwarded to `Blocks.launch`.
    """

    demo = build_dashboard(config_path)
    demo.launch(**launch_kwargs)


if __name__ == "__main__":
    launch_dashboard()
