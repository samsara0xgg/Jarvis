"""Tests for remote control protocol and skill."""

from remote.protocol import (
    ACTIONS,
    MSG_AUTH,
    MSG_COMMAND,
    MSG_RESULT,
    make_auth,
    make_command,
    make_result,
)


class TestProtocol:
    def test_make_command(self):
        msg = make_command("open_app", {"app_name": "Chrome"})
        assert msg["type"] == MSG_COMMAND
        assert msg["action"] == "open_app"
        assert msg["params"]["app_name"] == "Chrome"
        assert "request_id" in msg

    def test_make_command_custom_id(self):
        msg = make_command("lock_screen", request_id="abc123")
        assert msg["request_id"] == "abc123"

    def test_make_command_unknown_action(self):
        import pytest
        with pytest.raises(ValueError, match="Unknown action"):
            make_command("destroy_everything")

    def test_make_result_success(self):
        msg = make_result("req1", True, data="done")
        assert msg["type"] == MSG_RESULT
        assert msg["request_id"] == "req1"
        assert msg["success"] is True
        assert msg["data"] == "done"

    def test_make_result_failure(self):
        msg = make_result("req2", False, error="timeout")
        assert msg["success"] is False
        assert msg["error"] == "timeout"

    def test_make_auth(self):
        msg = make_auth("my_token")
        assert msg["type"] == MSG_AUTH
        assert msg["token"] == "my_token"

    def test_all_actions_defined(self):
        assert "open_app" in ACTIONS
        assert "screenshot" in ACTIONS
        assert "media_control" in ACTIONS
        assert len(ACTIONS) >= 10


class TestRemoteControlSkill:
    def test_skill_name(self):
        from skills.remote_control import RemoteControlSkill
        skill = RemoteControlSkill({})
        assert skill.skill_name == "remote_control"

    def test_required_role_is_owner(self):
        from skills.remote_control import RemoteControlSkill
        skill = RemoteControlSkill({})
        assert skill.get_required_role() == "owner"

    def test_tool_definitions(self):
        from skills.remote_control import RemoteControlSkill
        skill = RemoteControlSkill({})
        tools = skill.get_tool_definitions()
        names = {t["name"] for t in tools}
        assert "remote_open_app" in names
        assert "remote_screenshot" in names
        assert "remote_media" in names
        assert "remote_lock" in names
        assert "remote_volume" in names
        assert "remote_system_info" in names

    def test_execute_no_client(self):
        from skills.remote_control import RemoteControlSkill
        skill = RemoteControlSkill({})
        result = skill.execute("remote_open_app", {"app_name": "Chrome"})
        assert "not configured" in result or "unavailable" in result or "Failed" in result
