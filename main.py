"""Main CLI entrypoint for the smart-home voice lock end-to-end pipeline."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import json
import logging
from pathlib import Path
from typing import Any, Callable

import numpy as np
import yaml

from auth.enrollment import EnrollmentService
from auth.permission_manager import PermissionManager
from auth.user_store import UserStore
from core.audio_recorder import AudioRecorder
from core.command_parser import CommandParser
from core.speaker_encoder import SpeakerEncoder
from core.speaker_verifier import SpeakerVerifier, VerificationResult
from core.speech_recognizer import SpeechRecognizer, TranscriptionResult
from devices.device_manager import DeviceManager

LOGGER = logging.getLogger(__name__)
AUDIT_LOGGER_NAME = "smart_home_voice_lock.audit"
DEFAULT_COMMAND_RECORD_DURATION = 5.0
EXIT_COMMANDS = {"退出", "exit", "quit"}
ENROLL_COMMANDS = {"注册用户", "register", "enroll"}


@dataclass
class PipelineResult:
    """Structured result of one end-to-end voice interaction.

    Attributes:
        verification_result: Result of speaker verification when available.
        transcription_result: Result of ASR transcription when available.
        parsed_command: Parsed device command or parser error payload.
        permission_granted: Permission outcome when a device action was checked.
        execution_result: Final human-readable outcome for the interaction.
        user_display_name: Resolved user display name when available.
        should_continue: Whether the main loop should keep running.
    """

    verification_result: VerificationResult | None
    transcription_result: TranscriptionResult | None
    parsed_command: dict[str, Any] | None
    permission_granted: bool | None
    execution_result: str
    user_display_name: str | None
    should_continue: bool = True


class SmartHomeVoiceLockApp:
    """Coordinate recording, verification, ASR, command parsing, and device control."""

    def __init__(
        self,
        config: dict[str, Any],
        *,
        config_path: str | Path | None = None,
        audio_recorder: AudioRecorder | None = None,
        speaker_encoder: SpeakerEncoder | None = None,
        speaker_verifier: SpeakerVerifier | None = None,
        speech_recognizer: SpeechRecognizer | None = None,
        command_parser: CommandParser | None = None,
        device_manager: DeviceManager | None = None,
        permission_manager: PermissionManager | None = None,
        user_store: UserStore | None = None,
        enrollment_service: EnrollmentService | None = None,
        input_func: Callable[[str], str] = input,
        output_func: Callable[[str], None] = print,
        command_record_duration: float = DEFAULT_COMMAND_RECORD_DURATION,
    ) -> None:
        """Initialize the application and all dependent modules.

        Args:
            config: Parsed application configuration.
            config_path: Optional path to the YAML config file.
            audio_recorder: Optional injected audio recorder.
            speaker_encoder: Optional injected speaker encoder.
            speaker_verifier: Optional injected speaker verifier.
            speech_recognizer: Optional injected speech recognizer.
            command_parser: Optional injected command parser.
            device_manager: Optional injected device manager.
            permission_manager: Optional injected permission manager.
            user_store: Optional injected user store.
            enrollment_service: Optional injected enrollment service.
            input_func: Callable used for interactive input.
            output_func: Callable used for CLI output.
            command_record_duration: Fixed recording duration for normal commands.
        """

        self.config = config
        self.config_path = Path(config_path) if config_path is not None else Path(__file__).resolve().parent / "config.yaml"
        self.input_func = input_func
        self.output_func = output_func
        self.command_record_duration = float(command_record_duration)

        self.user_store = user_store or UserStore(config)
        self.audio_recorder = audio_recorder or AudioRecorder(config)
        self.speaker_encoder = speaker_encoder or SpeakerEncoder(config)
        self.speaker_verifier = speaker_verifier or SpeakerVerifier(
            config,
            self.speaker_encoder,
            self.user_store,
        )
        self.speech_recognizer = speech_recognizer or SpeechRecognizer(config)
        self.command_parser = command_parser or CommandParser(config)
        self.device_manager = device_manager or DeviceManager(config)
        self.permission_manager = permission_manager or PermissionManager()
        self.enrollment_service = enrollment_service or EnrollmentService(
            config,
            self.audio_recorder,
            self.speaker_encoder,
            self.user_store,
        )

        self.logger = LOGGER
        self.audit_logger = logging.getLogger(AUDIT_LOGGER_NAME)
        self._configure_audit_logging()

    def run(self) -> int:
        """Run the interactive CLI loop.

        Returns:
            Process exit code.
        """

        self._print_startup_banner()
        self.output_func("按回车开始录音，输入“注册用户”进入注册流程，输入“退出”结束程序。")

        while True:
            try:
                user_input = self.input_func("> ").strip()
            except EOFError:
                self.output_func("输入流已结束，程序退出。")
                return 0
            except KeyboardInterrupt:
                self.output_func("\n程序已中断。")
                return 0

            normalized_input = self._normalize_text(user_input)
            if normalized_input in EXIT_COMMANDS:
                self.output_func("程序已退出。")
                return 0
            if normalized_input in ENROLL_COMMANDS:
                self._run_enrollment_flow()
                continue
            if user_input:
                self.output_func("未识别的控制台命令。按回车开始录音，或输入“注册用户”/“退出”。")
                continue

            try:
                result = self.process_once()
            except KeyboardInterrupt:
                self.output_func("\n录音已取消。")
                continue
            except Exception as exc:
                self.logger.exception("Voice pipeline failed.")
                self.output_func(f"处理失败: {exc}")
                continue

            self.render_pipeline_result(result)
            if not result.should_continue:
                return 0

        return 0

    def process_once(self) -> PipelineResult:
        """Record one command utterance and run the full pipeline."""

        audio = self.audio_recorder.record(self.command_record_duration)
        return self.process_audio(audio)

    def process_audio(self, audio: np.ndarray) -> PipelineResult:
        """Run verification, ASR, parsing, permission, and execution on audio.

        Args:
            audio: Recorded waveform to process.

        Returns:
            The full pipeline result for the utterance.
        """

        verification_result: VerificationResult | None = None
        transcription_result: TranscriptionResult | None = None

        with ThreadPoolExecutor(max_workers=2) as executor:
            verify_future = executor.submit(self.speaker_verifier.verify, np.copy(audio))
            asr_future = executor.submit(self.speech_recognizer.transcribe, np.copy(audio))
            verification_result = self._resolve_future(verify_future, "speaker verification")
            transcription_result = self._resolve_future(asr_future, "speech recognition")

        normalized_text = self._normalize_text(transcription_result.text)
        if normalized_text in EXIT_COMMANDS:
            result = PipelineResult(
                verification_result=verification_result,
                transcription_result=transcription_result,
                parsed_command=None,
                permission_granted=None,
                execution_result="收到退出命令，程序即将退出。",
                user_display_name=self._resolve_user_display_name(verification_result.user if verification_result else None),
                should_continue=False,
            )
            self._log_pipeline_result(result)
            return result

        if normalized_text in ENROLL_COMMANDS:
            execution_result = self._run_enrollment_flow()
            result = PipelineResult(
                verification_result=verification_result,
                transcription_result=transcription_result,
                parsed_command=None,
                permission_granted=None,
                execution_result=execution_result,
                user_display_name=self._resolve_user_display_name(verification_result.user if verification_result else None),
            )
            self._log_pipeline_result(result)
            return result

        if not verification_result.verified:
            result = PipelineResult(
                verification_result=verification_result,
                transcription_result=transcription_result,
                parsed_command=None,
                permission_granted=False,
                execution_result="声纹验证失败，操作已拒绝。",
                user_display_name=None,
            )
            self._log_pipeline_result(result)
            return result

        user_id = verification_result.user
        user_record = self.user_store.get_user(user_id or "")
        user_display_name = self._resolve_user_display_name(user_id)
        user_role = str(user_record.get("role", "guest")) if user_record else "guest"
        parsed_command = self.command_parser.parse(transcription_result.text)

        if "error" in parsed_command:
            result = PipelineResult(
                verification_result=verification_result,
                transcription_result=transcription_result,
                parsed_command=parsed_command,
                permission_granted=False,
                execution_result=parsed_command["error"],
                user_display_name=user_display_name,
            )
            self._log_pipeline_result(result, user_role=user_role)
            return result

        if parsed_command.get("action") == "voice_shortcut":
            result = PipelineResult(
                verification_result=verification_result,
                transcription_result=transcription_result,
                parsed_command=parsed_command,
                permission_granted=True,
                execution_result=f"快捷指令已识别：{parsed_command.get('value')}",
                user_display_name=user_display_name,
            )
            self._log_pipeline_result(result, user_role=user_role)
            return result

        device = self.device_manager.get_device(str(parsed_command["device"]))
        permission_granted = self.permission_manager.check_permission(
            user_role,
            device,
            str(parsed_command["action"]),
        )
        if not permission_granted:
            result = PipelineResult(
                verification_result=verification_result,
                transcription_result=transcription_result,
                parsed_command=parsed_command,
                permission_granted=False,
                execution_result="权限不足，操作已拒绝。",
                user_display_name=user_display_name,
            )
            self._log_pipeline_result(result, user_role=user_role)
            return result

        execution_result = self.device_manager.execute_command(
            str(parsed_command["device"]),
            str(parsed_command["action"]),
            parsed_command.get("value"),
        )
        result = PipelineResult(
            verification_result=verification_result,
            transcription_result=transcription_result,
            parsed_command=parsed_command,
            permission_granted=True,
            execution_result=execution_result,
            user_display_name=user_display_name,
        )
        self._log_pipeline_result(result, user_role=user_role)
        return result

    def render_pipeline_result(self, result: PipelineResult) -> None:
        """Print one pipeline result in the CLI box format."""

        verification_line = self._format_verification_line(
            result.verification_result,
            result.user_display_name,
        )
        asr_text = result.transcription_result.text if result.transcription_result else ""
        parse_line = self._format_parsed_command(result.parsed_command)
        permission_line = self._format_permission_line(result.permission_granted)

        self.output_func(f"┌ 声纹验证: {verification_line}")
        self.output_func(f"│ ASR 文本: {json.dumps(asr_text, ensure_ascii=False)}")
        self.output_func(f"│ 指令解析: {parse_line}")
        self.output_func(f"│ 权限检查: {permission_line}")
        self.output_func(f"└ 执行结果: {result.execution_result}")

    def _run_enrollment_flow(self) -> str:
        """Run the interactive enrollment flow and return a status message."""

        user_id = self.input_func("请输入 user_id: ").strip()
        name = self.input_func("请输入用户姓名: ").strip()
        role = self.input_func("请输入角色（默认 resident）: ").strip() or None
        permissions_text = self.input_func("请输入权限列表，逗号分隔（可留空）: ").strip()
        permissions = [item.strip() for item in permissions_text.split(",") if item.strip()] or None

        if not user_id or not name:
            message = "注册取消：user_id 和姓名不能为空。"
            self.output_func(message)
            return message

        try:
            user_record = self.enrollment_service.enroll_user(
                user_id=user_id,
                name=name,
                role=role,
                permissions=permissions,
            )
        except Exception as exc:
            self.logger.exception("Enrollment flow failed.")
            message = f"注册失败: {exc}"
            self.output_func(message)
            return message

        message = f"注册成功：{user_record['name']} ({user_record['user_id']})"
        self.output_func(message)
        return message

    def _print_startup_banner(self) -> None:
        """Print startup summary information for the CLI."""

        user_count = len(self.user_store.get_all_users())
        device_count = len(self.device_manager.get_all_status())
        mode = str(self.config.get("devices", {}).get("mode", "sim")).lower()
        self.output_func(f"模式: {mode}")
        self.output_func(f"已注册用户数: {user_count}")
        self.output_func(f"设备数: {device_count}")

    def _resolve_future(self, future: Future[Any], task_name: str) -> Any:
        """Resolve a background future and re-raise failures with context."""

        try:
            return future.result()
        except Exception as exc:
            raise RuntimeError(f"{task_name} failed: {exc}") from exc

    def _resolve_user_display_name(self, user_id: str | None) -> str | None:
        """Map a verified user ID to a display name when available."""

        if not user_id:
            return None
        user_record = self.user_store.get_user(user_id)
        if user_record is None:
            return user_id
        return str(user_record.get("name") or user_id)

    def _format_verification_line(
        self,
        verification_result: VerificationResult | None,
        user_display_name: str | None,
    ) -> str:
        """Format the verification line shown in the CLI output."""

        if verification_result is None:
            return "❌ 未执行"

        if verification_result.verified:
            label = user_display_name or verification_result.user or "未知用户"
            return f"✅ {label} ({verification_result.confidence:.2f})"

        return f"❌ 未通过 ({verification_result.confidence:.2f})"

    def _format_parsed_command(self, parsed_command: dict[str, Any] | None) -> str:
        """Format parsed command dictionaries for CLI display."""

        if parsed_command is None:
            return "{}"

        parts = [f"{key}: {value}" for key, value in parsed_command.items()]
        return "{ " + ", ".join(parts) + " }"

    def _format_permission_line(self, permission_granted: bool | None) -> str:
        """Format the permission status shown in the CLI output."""

        if permission_granted is None:
            return "N/A"
        return "✅" if permission_granted else "❌"

    def _normalize_text(self, text: str) -> str:
        """Normalize text for special command matching."""

        return "".join(text.strip().lower().split())

    def _configure_audit_logging(self) -> None:
        """Attach a file handler for operation audit logs."""

        log_dir = self.config_path.parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "operations.log"

        self.audit_logger.setLevel(logging.INFO)
        self.audit_logger.propagate = False
        for handler in self.audit_logger.handlers:
            if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename) == log_path:
                return

        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        self.audit_logger.addHandler(handler)

    def _log_pipeline_result(
        self,
        result: PipelineResult,
        *,
        user_role: str | None = None,
    ) -> None:
        """Write a structured audit log line for one pipeline result."""

        payload = {
            "verification": {
                "verified": result.verification_result.verified if result.verification_result else None,
                "user": result.verification_result.user if result.verification_result else None,
                "confidence": result.verification_result.confidence if result.verification_result else None,
                "all_scores": result.verification_result.all_scores if result.verification_result else None,
            },
            "user_display_name": result.user_display_name,
            "user_role": user_role,
            "transcription": {
                "text": result.transcription_result.text if result.transcription_result else None,
                "language": result.transcription_result.language if result.transcription_result else None,
                "confidence": result.transcription_result.confidence if result.transcription_result else None,
            },
            "parsed_command": result.parsed_command,
            "permission_granted": result.permission_granted,
            "execution_result": result.execution_result,
            "should_continue": result.should_continue,
        }
        self.audit_logger.info(json.dumps(payload, ensure_ascii=False))


def load_config(config_path: str | Path) -> dict[str, Any]:
    """Load YAML config from disk.

    Args:
        config_path: Path to the YAML config file.

    Returns:
        Parsed config dictionary.
    """

    path = Path(config_path)
    with path.open("r", encoding="utf-8") as file_obj:
        payload = yaml.safe_load(file_obj)
    return payload or {}


def configure_logging(config: dict[str, Any]) -> None:
    """Configure root logging for the CLI app.

    Args:
        config: Parsed application configuration.
    """

    logging_config = config.get("logging", {})
    level_name = str(logging_config.get("level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)

    root_logger = logging.getLogger()
    if not root_logger.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        )
    else:
        root_logger.setLevel(level)


def main() -> int:
    """Run the smart-home voice lock CLI."""

    config_path = Path(__file__).resolve().parent / "config.yaml"
    config = load_config(config_path)
    configure_logging(config)

    try:
        app = SmartHomeVoiceLockApp(config, config_path=config_path)
    except Exception as exc:
        LOGGER.exception("Application initialization failed.")
        print(f"初始化失败: {exc}")
        return 1

    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
