"""Cross-skill contract regressions for high-risk Jarvis skill behavior."""

from __future__ import annotations

from unittest.mock import MagicMock

from core.response_channels import parse_response_channels
from core.tool_result import (
    FAILURE,
    SUCCESS,
    normalize_tool_result,
    parse_tool_result,
)
from tools import _EXECUTION_CONTEXT
import tools.reminders as reminders
import tools.smart_home as smart_home
import tools.time_utils as time_utils
import tools.todos as todos


def test_voice_document_channels_keep_code_out_of_voice() -> None:
    channels = parse_response_channels(
        "<voice>代码放在屏幕上。</voice>"
        "<document>```python\nprint('ok')\n```</document>"
    )

    assert channels.voice == "代码放在屏幕上。"
    assert "print(" not in channels.voice
    assert "print('ok')" in channels.document


def test_high_risk_legacy_plain_text_cannot_claim_typed() -> None:
    parsed = parse_tool_result(
        normalize_tool_result(
            "已输入",
            skill_name="type_to_focused",
            caller="llm",
            read_only=False,
            destructive=True,
            risk_level="high",
            action_type="macos_paste",
        )
    )

    assert parsed["status"] == FAILURE
    assert parsed["error_code"] == "untyped_side_effect_result"
    assert "typed" in parsed["claim_policy"]["forbidden_claims"]


def test_delivery_result_cannot_claim_task_completed() -> None:
    parsed = parse_tool_result(
        normalize_tool_result(
            "已投递",
            skill_name="cc_tell",
            caller="llm",
            read_only=False,
            destructive=True,
            risk_level="medium",
            action_type="zellij_send",
        )
    )

    assert parsed["status"] == SUCCESS
    assert parsed["outcome"]["type"] == "delivered"
    assert "message_delivered" in parsed["claim_policy"]["allowed_claims"]
    assert "task_completed" in parsed["claim_policy"]["forbidden_claims"]


def test_reminder_without_time_is_clarification_not_creation(tmp_path) -> None:
    reminders.init(filepath=str(tmp_path / "reminders.json"))
    _EXECUTION_CONTEXT["user_id"] = "contract_user"
    parsed = parse_tool_result(reminders.create_reminder("Bring homework"))

    assert parsed["status"] == "needs_clarification"
    assert parsed["error_code"] == "missing_due_at"
    assert "reminder_created" in parsed["claim_policy"]["forbidden_claims"]


def test_timer_success_has_fire_time_and_timer_claim() -> None:
    try:
        parsed = parse_tool_result(time_utils.set_timer(60, "contract"))
        assert parsed["status"] == SUCCESS
        assert parsed["data"]["timer_id"] == "contract_60"
        assert parsed["data"]["fires_at"]
        assert parsed["data"]["timezone"]
        assert parsed["claim_policy"]["allowed_claims"] == ["timer_created"]
    finally:
        time_utils.cancel_all()


def test_delete_todo_archives_and_never_claims_permanent_delete(tmp_path) -> None:
    todos.init(persist_dir=str(tmp_path / "todos"))
    _EXECUTION_CONTEXT["user_id"] = "contract_user"
    created = parse_tool_result(todos.add_todo("Contract archive"))
    todo_id = created["data"]["todo_id"]

    archived = parse_tool_result(todos.delete_todo(todo_id))

    assert archived["status"] == SUCCESS
    assert archived["outcome"]["type"] == "archived"
    assert archived["data"]["undo_token"]
    assert "todo_permanently_deleted" in archived["claim_policy"]["forbidden_claims"]


def test_smart_home_partial_group_cannot_claim_all_devices_changed() -> None:
    dm = MagicMock()
    pm = MagicMock()
    pm.check_permission.return_value = True
    devices = {
        "bedroom_lamp_1": _device("bedroom_lamp_1", "Bedroom Lamp 1"),
        "bedroom_lamp_2": _device("bedroom_lamp_2", "Bedroom Lamp 2"),
    }
    dm.get_device.side_effect = lambda device_id: devices[device_id]

    def execute(device_id: str, action: str, value: str | None) -> str:
        if device_id == "bedroom_lamp_2":
            raise RuntimeError("bridge timeout")
        return "ok"

    dm.execute_command.side_effect = execute
    smart_home.init(dm, pm)

    parsed = parse_tool_result(
        smart_home.smart_home_control(
            "bedroom_group",
            "turn_off",
            matched_alias="大灯",
            resolution_source="regex_router",
        )
    )

    assert parsed["status"] == "partial_success"
    assert len(parsed["data"]["successes"]) == 1
    assert len(parsed["data"]["failures"]) == 1
    assert "all_devices_changed" in parsed["claim_policy"]["forbidden_claims"]


def _device(device_id: str, name: str) -> MagicMock:
    device = MagicMock()
    device.device_id = device_id
    device.name = name
    device.device_type = "light"
    device.get_status.return_value = {"is_on": False, "brightness": 80}
    return device
