"""Tests for the YAMLInterpreter."""

import json
import os
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest
import yaml

from core.yaml_interpreter import YAMLInterpreter


def _make_skill(**overrides) -> dict:
    """Build a minimal valid skill dict with optional overrides."""
    skill = {
        "name": "get_weather",
        "description": "查询天气",
        "parameters": [
            {
                "name": "city",
                "type": "string",
                "description": "City name",
                "required": False,
                "default": "Victoria",
            },
        ],
        "action": {
            "type": "http_get",
            "url": "https://wttr.in/{{ city }}?format=j1",
            "headers": {"Accept-Language": "zh"},
            "timeout_ms": 10000,
            "retry": {"max": 3, "delay_ms": 1000, "backoff": "exponential"},
        },
        "response": {
            "extract": {"temp": "{{ result.current_condition[0].temp_C }}"},
            "compute": {},
            "template": "{{ city }} 现在 {{ temp }}°C",
            "error_template": "查询失败",
        },
        "security": {"allowed_domains": ["wttr.in"]},
    }
    skill.update(overrides)
    return skill


class TestToToolDefinition:
    def test_structure(self):
        interp = YAMLInterpreter()
        skill = _make_skill()
        td = interp.to_tool_definition(skill)
        assert td["name"] == "get_weather"
        assert td["description"] == "查询天气"
        schema = td["input_schema"]
        assert schema["type"] == "object"
        assert "city" in schema["properties"]
        assert schema["properties"]["city"]["type"] == "string"
        assert schema["properties"]["city"]["description"] == "City name"
        # city is not required
        assert "city" not in schema.get("required", [])

    def test_required_params(self):
        interp = YAMLInterpreter()
        skill = _make_skill(
            parameters=[
                {"name": "q", "type": "string", "description": "query", "required": True},
                {"name": "limit", "type": "integer", "description": "max", "required": False, "default": 10},
            ]
        )
        td = interp.to_tool_definition(skill)
        assert "q" in td["input_schema"]["required"]
        assert "limit" not in td["input_schema"]["required"]


class TestDomainWhitelist:
    def test_blocks_non_whitelisted_domain(self):
        interp = YAMLInterpreter()
        skill = _make_skill(
            action={
                "type": "http_get",
                "url": "https://evil.com/steal",
                "timeout_ms": 5000,
                "retry": {"max": 1, "delay_ms": 100, "backoff": "exponential"},
            },
            security={"allowed_domains": ["wttr.in"]},
        )
        result = interp.execute(skill, {"city": "Victoria"})
        assert "blocked" in result.lower() or "不允许" in result.lower() or "not allowed" in result.lower()


class TestPrivateIPBlocked:
    def test_localhost_blocked(self):
        interp = YAMLInterpreter()
        skill = _make_skill(
            action={
                "type": "http_get",
                "url": "http://127.0.0.1:8080/api",
                "timeout_ms": 5000,
                "retry": {"max": 1, "delay_ms": 100, "backoff": "exponential"},
            },
            security={"allowed_domains": ["127.0.0.1"]},
        )
        result = interp.execute(skill, {})
        assert "blocked" in result.lower() or "private" in result.lower()

    def test_10_network_blocked(self):
        interp = YAMLInterpreter()
        skill = _make_skill(
            action={
                "type": "http_get",
                "url": "http://10.0.0.1/api",
                "timeout_ms": 5000,
                "retry": {"max": 1, "delay_ms": 100, "backoff": "exponential"},
            },
            security={"allowed_domains": ["10.0.0.1"]},
        )
        result = interp.execute(skill, {})
        assert "blocked" in result.lower() or "private" in result.lower()

    def test_192_168_blocked(self):
        interp = YAMLInterpreter()
        skill = _make_skill(
            action={
                "type": "http_get",
                "url": "http://192.168.1.1/api",
                "timeout_ms": 5000,
                "retry": {"max": 1, "delay_ms": 100, "backoff": "exponential"},
            },
            security={"allowed_domains": ["192.168.1.1"]},
        )
        result = interp.execute(skill, {})
        assert "blocked" in result.lower() or "private" in result.lower()

    def test_172_16_blocked(self):
        interp = YAMLInterpreter()
        skill = _make_skill(
            action={
                "type": "http_get",
                "url": "http://172.16.0.1/api",
                "timeout_ms": 5000,
                "retry": {"max": 1, "delay_ms": 100, "backoff": "exponential"},
            },
            security={"allowed_domains": ["172.16.0.1"]},
        )
        result = interp.execute(skill, {})
        assert "blocked" in result.lower() or "private" in result.lower()


class TestExecuteSuccess:
    @patch("core.yaml_interpreter.requests")
    def test_http_get_renders_response(self, mock_requests):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "current_condition": [{"temp_C": "15"}],
        }
        mock_requests.get.return_value = mock_resp

        interp = YAMLInterpreter()
        skill = _make_skill()
        result = interp.execute(skill, {"city": "Vancouver"})

        assert "Vancouver" in result
        assert "15" in result
        mock_requests.get.assert_called_once()
        call_args = mock_requests.get.call_args
        assert "Vancouver" in call_args[0][0]

    @patch("core.yaml_interpreter.requests")
    def test_http_post(self, mock_requests):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"status": "ok"}
        mock_requests.post.return_value = mock_resp

        interp = YAMLInterpreter()
        skill = _make_skill(
            action={
                "type": "http_post",
                "url": "https://api.example.com/run",
                "body": {"query": "{{ q }}"},
                "timeout_ms": 5000,
                "retry": {"max": 1, "delay_ms": 100, "backoff": "exponential"},
            },
            response={
                "extract": {"status": "{{ result.status }}"},
                "compute": {},
                "template": "Result: {{ status }}",
                "error_template": "Failed",
            },
            security={"allowed_domains": ["api.example.com"]},
        )
        result = interp.execute(skill, {"q": "hello"})
        assert "ok" in result
        mock_requests.post.assert_called_once()


class TestRetryOnFailure:
    @patch("core.yaml_interpreter.time.sleep")
    @patch("core.yaml_interpreter.requests")
    def test_retries_then_succeeds(self, mock_requests, mock_sleep):
        fail_resp = MagicMock()
        fail_resp.ok = False
        fail_resp.status_code = 500
        fail_resp.raise_for_status.side_effect = Exception("Server Error")

        ok_resp = MagicMock()
        ok_resp.ok = True
        ok_resp.json.return_value = {
            "current_condition": [{"temp_C": "20"}],
        }

        mock_requests.get.side_effect = [fail_resp, fail_resp, ok_resp]

        interp = YAMLInterpreter()
        skill = _make_skill()
        result = interp.execute(skill, {"city": "Victoria"})

        assert "20" in result
        assert mock_requests.get.call_count == 3


class TestErrorTemplate:
    @patch("core.yaml_interpreter.time.sleep")
    @patch("core.yaml_interpreter.requests")
    def test_all_retries_fail_returns_error_template(self, mock_requests, mock_sleep):
        fail_resp = MagicMock()
        fail_resp.ok = False
        fail_resp.status_code = 503
        fail_resp.raise_for_status.side_effect = Exception("Unavailable")

        mock_requests.get.side_effect = [fail_resp, fail_resp, fail_resp]

        interp = YAMLInterpreter()
        skill = _make_skill()
        result = interp.execute(skill, {"city": "Victoria"})

        assert result == "查询失败"
        assert mock_requests.get.call_count == 3

    @patch("core.yaml_interpreter.time.sleep")
    @patch("core.yaml_interpreter.requests")
    def test_no_error_template_returns_fallback(self, mock_requests, mock_sleep):
        fail_resp = MagicMock()
        fail_resp.ok = False
        fail_resp.raise_for_status.side_effect = Exception("Unavailable")

        mock_requests.get.side_effect = [fail_resp]

        interp = YAMLInterpreter()
        skill = _make_skill(
            action={
                "type": "http_get",
                "url": "https://wttr.in/{{ city }}",
                "timeout_ms": 5000,
                "retry": {"max": 1, "delay_ms": 100, "backoff": "exponential"},
            },
        )
        # Remove error_template
        skill["response"] = {
            "extract": {},
            "compute": {},
            "template": "{{ city }}",
        }
        result = interp.execute(skill, {"city": "Victoria"})
        # Layer 3: fallback string
        assert "失败" in result or "fail" in result.lower() or "error" in result.lower()


class TestLoadSkill:
    def test_load_from_yaml_file(self):
        skill_data = {
            "name": "test_skill",
            "description": "A test",
            "parameters": [],
            "action": {
                "type": "http_get",
                "url": "https://example.com",
                "timeout_ms": 5000,
                "retry": {"max": 1, "delay_ms": 100, "backoff": "exponential"},
            },
            "response": {
                "extract": {},
                "compute": {},
                "template": "done",
                "error_template": "fail",
            },
            "security": {"allowed_domains": ["example.com"]},
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(skill_data, f)
            f.flush()
            path = f.name

        try:
            interp = YAMLInterpreter()
            loaded = interp.load_skill(path)
            assert loaded["name"] == "test_skill"
            assert loaded["description"] == "A test"
            assert loaded["action"]["type"] == "http_get"
        finally:
            os.unlink(path)


class TestParameterDefaults:
    @patch("core.yaml_interpreter.requests")
    def test_missing_param_gets_default(self, mock_requests):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "current_condition": [{"temp_C": "10"}],
        }
        mock_requests.get.return_value = mock_resp

        interp = YAMLInterpreter()
        skill = _make_skill()
        # Don't pass city — should default to "Victoria"
        result = interp.execute(skill, {})

        assert "Victoria" in result
        call_url = mock_requests.get.call_args[0][0]
        assert "Victoria" in call_url

    @patch("core.yaml_interpreter.requests")
    def test_explicit_param_overrides_default(self, mock_requests):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "current_condition": [{"temp_C": "25"}],
        }
        mock_requests.get.return_value = mock_resp

        interp = YAMLInterpreter()
        skill = _make_skill()
        result = interp.execute(skill, {"city": "Toronto"})

        assert "Toronto" in result
        call_url = mock_requests.get.call_args[0][0]
        assert "Toronto" in call_url


class TestComputeStep:
    @patch("core.yaml_interpreter.requests")
    def test_compute_with_float_conversion(self, mock_requests):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"rates": {"JPY": 150.123456}}
        mock_requests.get.return_value = mock_resp

        interp = YAMLInterpreter()
        skill = _make_skill(
            parameters=[
                {"name": "amount", "type": "number", "description": "Amount", "required": True},
                {"name": "base", "type": "string", "description": "Base currency", "required": True},
                {"name": "target", "type": "string", "description": "Target currency", "required": True},
            ],
            action={
                "type": "http_get",
                "url": "https://api.exchangerate.host/latest?base={{ base }}",
                "timeout_ms": 5000,
                "retry": {"max": 1, "delay_ms": 100, "backoff": "exponential"},
            },
            response={
                "extract": {"rate": "{{ result.rates[target] }}"},
                "compute": {"converted": "{{ amount * rate | round(4) }}"},
                "template": "{{ amount }} {{ base }} = {{ converted }} {{ target }}",
                "error_template": "汇率查询失败",
            },
            security={"allowed_domains": ["api.exchangerate.host"]},
        )
        result = interp.execute(skill, {"amount": 100, "base": "USD", "target": "JPY"})
        assert "USD" in result
        assert "JPY" in result
        # Jinja2: `amount * rate | round(4)` = amount * round(rate, 4)
        # = 100 * 150.1235 = 15012.35
        assert "15012.35" in result


class TestAuthEnv:
    @patch("core.yaml_interpreter.requests")
    def test_auth_env_adds_header(self, mock_requests):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"data": "ok"}
        mock_requests.get.return_value = mock_resp

        interp = YAMLInterpreter()
        skill = _make_skill(
            action={
                "type": "http_get",
                "url": "https://api.example.com/data",
                "timeout_ms": 5000,
                "retry": {"max": 1, "delay_ms": 100, "backoff": "exponential"},
            },
            response={
                "extract": {"d": "{{ result.data }}"},
                "compute": {},
                "template": "{{ d }}",
                "error_template": "fail",
            },
            security={
                "allowed_domains": ["api.example.com"],
                "auth_env": "TEST_API_KEY",
            },
        )

        with patch.dict(os.environ, {"TEST_API_KEY": "secret123"}):
            interp.execute(skill, {})

        call_kwargs = mock_requests.get.call_args
        headers = call_kwargs[1]["headers"] if "headers" in call_kwargs[1] else call_kwargs.kwargs.get("headers", {})
        assert headers.get("Authorization") == "Bearer secret123"


# ===========================================================================
# Slug filter
# ===========================================================================


class TestSlugFilter:
    def test_ascii_text_to_kebab(self):
        from core.yaml_interpreter import _slug_filter
        assert _slug_filter("Dentist Reminder") == "dentist-reminder"

    def test_caps_at_five_words(self):
        from core.yaml_interpreter import _slug_filter
        out = _slug_filter("one two three four five six seven eight")
        assert out == "one-two-three-four-five"

    def test_caps_at_sixty_chars(self):
        from core.yaml_interpreter import _slug_filter
        out = _slug_filter("aaaa " * 30)
        assert len(out) <= 60

    def test_chinese_only_falls_back_to_literal(self):
        from core.yaml_interpreter import _slug_filter
        out = _slug_filter("提醒看牙医")
        assert "提醒" in out
        assert out == "提醒看牙医"

    def test_chinese_strips_whitespace(self):
        from core.yaml_interpreter import _slug_filter
        out = _slug_filter("提  醒\n看\t牙医")
        assert out == "提醒看牙医"

    def test_chinese_strips_filesystem_unsafe_chars(self):
        from core.yaml_interpreter import _slug_filter
        out = _slug_filter("提/醒\\看*牙?医:笔<记>|")
        assert "/" not in out and "\\" not in out and "*" not in out
        assert "?" not in out and ":" not in out and "<" not in out
        assert ">" not in out and "|" not in out

    def test_empty_string_to_note(self):
        from core.yaml_interpreter import _slug_filter
        assert _slug_filter("") == "note"

    def test_whitespace_only_to_note(self):
        from core.yaml_interpreter import _slug_filter
        assert _slug_filter("   \t\n") == "note"

    def test_none_to_note(self):
        from core.yaml_interpreter import _slug_filter
        assert _slug_filter(None) == "note"

    def test_works_inside_jinja_render(self):
        interp = YAMLInterpreter()
        out = interp._render("{{ x | slug }}", {"x": "Hello World"})
        assert out == "hello-world"


# ===========================================================================
# now() global
# ===========================================================================


class TestNowGlobal:
    def test_default_format_matches_jarvis_schema(self):
        import re
        interp = YAMLInterpreter()
        out = interp._render("{{ now() }}", {})
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}-\d{4}", out)

    def test_custom_format(self):
        interp = YAMLInterpreter()
        out = interp._render("{{ now('%Y') }}", {})
        assert len(out) == 4
        assert out.isdigit()


# ===========================================================================
# file_write action — happy paths
# ===========================================================================


def _file_write_skill(tmp_root, **action_overrides):
    """Build a minimal file_write skill rooted at tmp_root."""
    action = {
        "type": "file_write",
        "allowed_root": str(tmp_root),
        "path": str(tmp_root) + "/{{ name }}.txt",
        "content": "{{ body }}",
        "create_parents": True,
    }
    action.update(action_overrides)
    return {
        "name": "test_write",
        "description": "Write a file",
        "parameters": [
            {"name": "name", "type": "string", "required": True},
            {"name": "body", "type": "string", "required": True},
        ],
        "action": action,
        "response": {
            "template": "Saved: {{ filename }}",
            "error_template": "fail",
        },
    }


class TestFileWriteHappyPath:
    def test_writes_content_to_path(self, tmp_path):
        skill = _file_write_skill(tmp_path)
        out = YAMLInterpreter().execute(skill, {"name": "hello", "body": "world"})
        assert out == "Saved: hello.txt"
        assert (tmp_path / "hello.txt").read_text() == "world"

    def test_creates_parent_dirs(self, tmp_path):
        skill = _file_write_skill(
            tmp_path,
            path=str(tmp_path) + "/sub/dir/{{ name }}.txt",
        )
        YAMLInterpreter().execute(skill, {"name": "x", "body": "y"})
        assert (tmp_path / "sub" / "dir" / "x.txt").read_text() == "y"

    def test_create_parents_false_missing_dir_falls_back(self, tmp_path):
        skill = _file_write_skill(
            tmp_path,
            path=str(tmp_path) + "/missing/{{ name }}.txt",
            create_parents=False,
        )
        out = YAMLInterpreter().execute(skill, {"name": "x", "body": "y"})
        assert out == "fail"
        assert not (tmp_path / "missing").exists()

    def test_response_template_has_file_path_and_filename(self, tmp_path):
        skill = _file_write_skill(tmp_path)
        skill["response"]["template"] = "{{ file_path }} :: {{ filename }}"
        out = YAMLInterpreter().execute(skill, {"name": "n", "body": "b"})
        assert out == f"{tmp_path}/n.txt :: n.txt"

    def test_overwrites_existing_file(self, tmp_path):
        (tmp_path / "x.txt").write_text("old")
        skill = _file_write_skill(tmp_path)
        YAMLInterpreter().execute(skill, {"name": "x", "body": "new"})
        assert (tmp_path / "x.txt").read_text() == "new"


# ===========================================================================
# file_write action — security
# ===========================================================================


class TestFileWritePathSafety:
    def test_dotdot_escape_blocked(self, tmp_path):
        skill = _file_write_skill(
            tmp_path,
            path=str(tmp_path) + "/{{ name }}.txt",
        )
        out = YAMLInterpreter().execute(
            skill, {"name": "../../../etc/passwd_pwn", "body": "x"}
        )
        assert "Path escape blocked" in out

    def test_absolute_path_outside_root_blocked(self, tmp_path):
        skill = _file_write_skill(tmp_path, path="/tmp/outside_pwn.txt")
        out = YAMLInterpreter().execute(skill, {"name": "x", "body": "y"})
        assert "Path escape blocked" in out
        assert not os.path.exists("/tmp/outside_pwn.txt")

    def test_symlink_escape_blocked(self, tmp_path):
        outside = tmp_path.parent / "outside_target"
        outside.mkdir()
        link = tmp_path / "evil_link"
        link.symlink_to(outside)
        skill = _file_write_skill(
            tmp_path,
            path=str(link) + "/{{ name }}.txt",
        )
        out = YAMLInterpreter().execute(skill, {"name": "x", "body": "y"})
        assert "Path escape blocked" in out
        assert not (outside / "x.txt").exists()

    def test_missing_allowed_root_blocked(self, tmp_path):
        skill = _file_write_skill(tmp_path)
        del skill["action"]["allowed_root"]
        out = YAMLInterpreter().execute(skill, {"name": "x", "body": "y"})
        assert "allowed_root" in out

    def test_missing_path_blocked(self, tmp_path):
        skill = _file_write_skill(tmp_path)
        del skill["action"]["path"]
        out = YAMLInterpreter().execute(skill, {"name": "x", "body": "y"})
        assert "path" in out.lower()


# ===========================================================================
# Real obsidian_inbox.yaml end-to-end
# ===========================================================================


class TestObsidianInboxYAMLSkill:
    def _load(self, monkeypatch, tmp_path):
        """Load the real yaml but redirect inbox to tmp_path."""
        interp = YAMLInterpreter()
        skill_path = os.path.join(
            os.path.dirname(__file__), "..", "skills", "obsidian_inbox.yaml"
        )
        skill = interp.load_skill(skill_path)
        skill["action"]["allowed_root"] = str(tmp_path)
        skill["action"]["path"] = (
            str(tmp_path) + "/{{ now() }}-{{ (title or content[:80]) | slug }}.md"
        )
        return interp, skill

    def test_loads_correct_schema(self):
        interp = YAMLInterpreter()
        skill_path = os.path.join(
            os.path.dirname(__file__), "..", "skills", "obsidian_inbox.yaml"
        )
        skill = interp.load_skill(skill_path)
        td = interp.to_tool_definition(skill)
        assert td["name"] == "obsidian_add_to_inbox"
        assert "inbox" in td["description"].lower()
        assert "content" in td["input_schema"]["properties"]
        assert td["input_schema"]["required"] == ["content"]

    def test_writes_content_only_no_title(self, monkeypatch, tmp_path):
        interp, skill = self._load(monkeypatch, tmp_path)
        out = interp.execute(skill, {"content": "hello yaml world"})
        assert "Saved to inbox" in out
        files = list(tmp_path.iterdir())
        assert len(files) == 1
        assert files[0].read_text() == "hello yaml world"

    def test_writes_with_title_h1_format(self, monkeypatch, tmp_path):
        interp, skill = self._load(monkeypatch, tmp_path)
        interp.execute(skill, {"content": "body line", "title": "My Tag"})
        files = list(tmp_path.iterdir())
        assert len(files) == 1
        text = files[0].read_text()
        assert text.startswith("# My Tag")
        assert "body line" in text

    def test_filename_starts_with_timestamp(self, monkeypatch, tmp_path):
        import re
        interp, skill = self._load(monkeypatch, tmp_path)
        interp.execute(skill, {"content": "x", "title": "test"})
        fname = next(tmp_path.iterdir()).name
        assert re.match(r"^\d{4}-\d{2}-\d{2}-\d{4}-", fname)
        assert fname.endswith(".md")

    def test_chinese_content_no_title(self, monkeypatch, tmp_path):
        interp, skill = self._load(monkeypatch, tmp_path)
        interp.execute(skill, {"content": "明天去买菜"})
        files = list(tmp_path.iterdir())
        assert len(files) == 1
        assert "明天去买菜" in files[0].name
        assert files[0].read_text() == "明天去买菜"

    def test_malicious_title_with_dotdot_safe(self, monkeypatch, tmp_path):
        """LLM-supplied title containing path traversal must not escape inbox."""
        interp, skill = self._load(monkeypatch, tmp_path)
        out = interp.execute(
            skill,
            {"content": "x", "title": "../../etc/passwd"},
        )
        # slug filter strips slashes, so file lands safely inside tmp_path
        assert "Saved to inbox" in out or "Path escape blocked" in out
        outside = tmp_path.parent / "etc"
        assert not outside.exists()


# ===========================================================================
# macos_paste action — mocked subprocess
# ===========================================================================


def _macos_paste_skill(**action_overrides):
    """Build a minimal macos_paste skill."""
    action = {"type": "macos_paste", "text": "{{ text }}"}
    action.update(action_overrides)
    return {
        "name": "test_paste",
        "description": "Paste text to focused app",
        "parameters": [{"name": "text", "type": "string", "required": True}],
        "action": action,
        "response": {
            "template": "Pasted: {{ text }}",
            "error_template": "fail",
        },
    }


class TestMacOSPasteAction:
    def test_pbcopy_receives_rendered_text(self):
        skill = _macos_paste_skill()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            YAMLInterpreter().execute(skill, {"text": "你好 world"})
        # First call: pbcopy with text on stdin
        first = mock_run.call_args_list[0]
        assert first.args[0] == ["pbcopy"]
        assert first.kwargs.get("input") == "你好 world"
        assert first.kwargs.get("text") is True

    def test_osascript_called_with_fixed_paste_command(self):
        from core.yaml_interpreter import _APPLESCRIPT_PASTE
        skill = _macos_paste_skill()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            YAMLInterpreter().execute(skill, {"text": "x"})
        # Second call: osascript -e <fixed AppleScript>
        second = mock_run.call_args_list[1]
        assert second.args[0] == ["osascript", "-e", _APPLESCRIPT_PASTE]

    def test_user_text_never_appears_in_subprocess_argv(self):
        """Injection guard: text must not flow into argv, only into stdin."""
        skill = _macos_paste_skill()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            YAMLInterpreter().execute(
                skill, {"text": '"; do evil; tell app to "'}
            )
        for call in mock_run.call_args_list:
            argv = call.args[0]
            # text appears nowhere in command-line arguments
            assert all(
                'do evil' not in str(arg) for arg in argv
            ), f"text leaked into argv: {argv}"

    def test_response_template_rendered_with_text(self):
        skill = _macos_paste_skill()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            out = YAMLInterpreter().execute(skill, {"text": "hello"})
        assert out == "Pasted: hello"

    def test_empty_text_short_circuits_no_subprocess(self):
        skill = _macos_paste_skill()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            out = YAMLInterpreter().execute(skill, {"text": ""})
        assert "无内容" in out
        mock_run.assert_not_called()

    def test_subprocess_timeout_returns_error_template(self):
        import subprocess
        skill = _macos_paste_skill()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="pbcopy", timeout=2)
            out = YAMLInterpreter().execute(skill, {"text": "x"})
        assert out == "fail"

    def test_called_process_error_returns_error_template(self):
        import subprocess
        skill = _macos_paste_skill()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, ["osascript"])
            out = YAMLInterpreter().execute(skill, {"text": "x"})
        assert out == "fail"

    def test_file_not_found_returns_error_template(self):
        skill = _macos_paste_skill()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("pbcopy")
            out = YAMLInterpreter().execute(skill, {"text": "x"})
        assert out == "fail"

    def test_file_not_found_no_error_template_returns_not_macos(self):
        skill = _macos_paste_skill()
        skill["response"].pop("error_template")
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("pbcopy")
            out = YAMLInterpreter().execute(skill, {"text": "x"})
        assert "Not on macOS" in out


# ===========================================================================
# Real type_to_focused.yaml end-to-end (mocked subprocess)
# ===========================================================================


class TestTypeToFocusedYAMLSkill:
    def _load(self):
        interp = YAMLInterpreter()
        skill_path = os.path.join(
            os.path.dirname(__file__), "..", "skills", "type_to_focused.yaml"
        )
        return interp, interp.load_skill(skill_path)

    def test_loads_correct_schema(self):
        interp, skill = self._load()
        td = interp.to_tool_definition(skill)
        assert td["name"] == "type_to_focused"
        assert td["input_schema"]["required"] == ["text"]
        # Trigger anchors documented in description
        desc = td["description"].lower()
        for anchor in ("帮我输入", "type this into cc"):
            assert anchor.lower() in desc

    def test_pastes_text_via_clipboard(self):
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            out = interp.execute(skill, {"text": "测试输入"})
        assert mock_run.call_count == 2
        first = mock_run.call_args_list[0]
        assert first.args[0] == ["pbcopy"]
        assert first.kwargs["input"] == "测试输入"
        assert "已输入" in out

    def test_long_text_truncated_in_response(self):
        interp, skill = self._load()
        long_text = "a" * 100
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            out = interp.execute(skill, {"text": long_text})
        assert "..." in out
        # Response is truncated to ~30 chars + ellipsis
        assert len(out) < 80

    def test_short_text_not_truncated(self):
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            out = interp.execute(skill, {"text": "hi"})
        assert "..." not in out


# ===========================================================================
# macos_paste target param — focus control
# ===========================================================================


class TestMacOSPasteTarget:
    def _skill_with_target(self):
        return {
            "name": "test_paste",
            "description": "paste",
            "parameters": [
                {"name": "text", "type": "string", "required": True},
                {"name": "target", "type": "string", "required": False, "default": ""},
            ],
            "action": {
                "type": "macos_paste",
                "text": "{{ text }}",
                "target": "{{ target }}",
            },
            "response": {"template": "ok", "error_template": "fail"},
        }

    def test_empty_target_keeps_v1_path(self):
        """No target → 2 subprocess calls: pbcopy + plain paste."""
        from core.yaml_interpreter import _APPLESCRIPT_PASTE
        skill = self._skill_with_target()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            YAMLInterpreter().execute(skill, {"text": "x", "target": ""})
        assert mock_run.call_count == 2
        argv = mock_run.call_args_list[1].args[0]
        assert argv == ["osascript", "-e", _APPLESCRIPT_PASTE]

    def test_target_app_uses_argv_binding(self):
        """target='Cursor' → activate via 'item 1 of argv', name as positional arg."""
        skill = self._skill_with_target()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            YAMLInterpreter().execute(skill, {"text": "x", "target": "Cursor"})
        argv = mock_run.call_args_list[1].args[0]
        # App name appears as final positional arg (after all -e fragments)
        assert argv[-1] == "Cursor"
        # AppleScript uses argv binding, not interpolation
        e_fragments = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
        assert any("item 1 of argv" in f for f in e_fragments)
        # The name itself is NOT inside any -e fragment
        for f in e_fragments:
            assert "Cursor" not in f

    def test_malicious_target_no_injection(self):
        """AppleScript metachars in target stay as a positional arg only."""
        skill = self._skill_with_target()
        nasty = '"; do shell script "rm -rf ~"; tell app "'
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            YAMLInterpreter().execute(skill, {"text": "x", "target": nasty})
        argv = mock_run.call_args_list[1].args[0]
        assert argv[-1] == nasty
        e_fragments = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
        for f in e_fragments:
            assert "rm -rf" not in f
            assert "do shell script" not in f

    def test_target_prev_uses_cmdtab(self):
        from core.yaml_interpreter import _APPLESCRIPT_CMDTAB
        skill = self._skill_with_target()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            YAMLInterpreter().execute(skill, {"text": "x", "target": "prev"})
        argv = mock_run.call_args_list[1].args[0]
        e_fragments = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
        assert _APPLESCRIPT_CMDTAB in e_fragments
        # No argv binding in prev mode
        assert "on run argv" not in e_fragments

    def test_target_chinese_prev_keyword(self):
        from core.yaml_interpreter import _APPLESCRIPT_CMDTAB
        skill = self._skill_with_target()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            YAMLInterpreter().execute(skill, {"text": "x", "target": "上一个"})
        e_fragments = [
            mock_run.call_args_list[1].args[0][i + 1]
            for i, a in enumerate(mock_run.call_args_list[1].args[0])
            if a == "-e"
        ]
        assert _APPLESCRIPT_CMDTAB in e_fragments

    def test_target_case_insensitive_prev(self):
        from core.yaml_interpreter import _APPLESCRIPT_CMDTAB
        skill = self._skill_with_target()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            YAMLInterpreter().execute(skill, {"text": "x", "target": "PREVIOUS"})
        e_fragments = [
            mock_run.call_args_list[1].args[0][i + 1]
            for i, a in enumerate(mock_run.call_args_list[1].args[0])
            if a == "-e"
        ]
        assert _APPLESCRIPT_CMDTAB in e_fragments

    def test_response_template_renders_target(self):
        skill = self._skill_with_target()
        skill["response"]["template"] = "→ {{ target }}: {{ text }}"
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            out = YAMLInterpreter().execute(skill, {"text": "hi", "target": "iTerm"})
        assert out == "→ iTerm: hi"

    def test_target_activate_failure_returns_error_template(self):
        import subprocess
        skill = self._skill_with_target()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            # pbcopy succeeds, osascript activate-+-paste fails
            mock_run.side_effect = [
                MagicMock(returncode=0),
                subprocess.CalledProcessError(1, ["osascript"]),
            ]
            out = YAMLInterpreter().execute(skill, {"text": "x", "target": "NopeApp"})
        assert out == "fail"

    def test_build_paste_argv_pure_function(self):
        """_build_paste_argv is a pure helper — exercise all 3 branches directly."""
        from core.yaml_interpreter import (
            YAMLInterpreter,
            _APPLESCRIPT_CMDTAB,
            _APPLESCRIPT_PASTE,
        )
        # Empty
        empty = YAMLInterpreter._build_paste_argv("")
        assert empty == ["osascript", "-e", _APPLESCRIPT_PASTE]
        # prev
        prev = YAMLInterpreter._build_paste_argv("prev")
        assert _APPLESCRIPT_CMDTAB in prev
        assert "on run argv" not in prev
        # app name
        app = YAMLInterpreter._build_paste_argv("TextEdit")
        assert app[-1] == "TextEdit"
        assert "on run argv" in app


# ===========================================================================
# Case-insensitive app-name resolution
# ===========================================================================


class TestResolveAppName:
    def test_canonical_name_unchanged(self, tmp_path, monkeypatch):
        from core.yaml_interpreter import _resolve_app_name
        (tmp_path / "Cursor.app").mkdir()
        monkeypatch.setattr("core.yaml_interpreter._APP_DIRS", (str(tmp_path),))
        assert _resolve_app_name("Cursor") == "Cursor"

    def test_lowercase_resolves_to_canonical(self, tmp_path, monkeypatch):
        from core.yaml_interpreter import _resolve_app_name
        (tmp_path / "Cursor.app").mkdir()
        monkeypatch.setattr("core.yaml_interpreter._APP_DIRS", (str(tmp_path),))
        assert _resolve_app_name("cursor") == "Cursor"

    def test_uppercase_resolves_to_canonical(self, tmp_path, monkeypatch):
        from core.yaml_interpreter import _resolve_app_name
        (tmp_path / "iTerm.app").mkdir()
        monkeypatch.setattr("core.yaml_interpreter._APP_DIRS", (str(tmp_path),))
        assert _resolve_app_name("ITERM") == "iTerm"

    def test_multiword_app_name(self, tmp_path, monkeypatch):
        from core.yaml_interpreter import _resolve_app_name
        (tmp_path / "Visual Studio Code.app").mkdir()
        monkeypatch.setattr("core.yaml_interpreter._APP_DIRS", (str(tmp_path),))
        assert _resolve_app_name("visual studio code") == "Visual Studio Code"

    def test_unknown_app_passes_through(self, tmp_path, monkeypatch):
        from core.yaml_interpreter import _resolve_app_name
        monkeypatch.setattr("core.yaml_interpreter._APP_DIRS", (str(tmp_path),))
        assert _resolve_app_name("DefinitelyNotInstalled") == "DefinitelyNotInstalled"

    def test_missing_app_dir_safe(self, monkeypatch):
        from core.yaml_interpreter import _resolve_app_name
        monkeypatch.setattr("core.yaml_interpreter._APP_DIRS", ("/this/does/not/exist",))
        # falls through to original
        assert _resolve_app_name("anything") == "anything"

    def test_empty_target_unchanged(self):
        from core.yaml_interpreter import _resolve_app_name
        assert _resolve_app_name("") == ""

    def test_resolve_used_in_build_argv(self, tmp_path, monkeypatch):
        """End-to-end: build_paste_argv passes the resolved canonical name."""
        from core.yaml_interpreter import YAMLInterpreter
        (tmp_path / "Cursor.app").mkdir()
        monkeypatch.setattr("core.yaml_interpreter._APP_DIRS", (str(tmp_path),))
        argv = YAMLInterpreter._build_paste_argv("cursor")
        assert argv[-1] == "Cursor"


# ===========================================================================
# Real type_to_focused.yaml v2 — target field end-to-end
# ===========================================================================


class TestTypeToFocusedV2Target:
    def _load(self):
        interp = YAMLInterpreter()
        skill_path = os.path.join(
            os.path.dirname(__file__), "..", "skills", "type_to_focused.yaml"
        )
        return interp, interp.load_skill(skill_path)

    def test_schema_has_target_optional(self):
        interp, skill = self._load()
        td = interp.to_tool_definition(skill)
        assert "target" in td["input_schema"]["properties"]
        assert "target" not in td["input_schema"].get("required", [])

    def test_yaml_targets_iterm(self):
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            out = interp.execute(skill, {"text": "x", "target": "iTerm"})
        argv = mock_run.call_args_list[1].args[0]
        assert argv[-1] == "iTerm"
        assert "→ iTerm" in out

    def test_yaml_default_target_empty(self):
        from core.yaml_interpreter import _APPLESCRIPT_PASTE
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            out = interp.execute(skill, {"text": "x"})
        argv = mock_run.call_args_list[1].args[0]
        assert argv == ["osascript", "-e", _APPLESCRIPT_PASTE]
        assert "→" not in out  # no target arrow when target empty


# ===========================================================================
# zellij_send action — direct injection into a zellij session pane
# ===========================================================================


def _zellij_send_skill(**action_overrides):
    """Build a minimal zellij_send skill."""
    action = {"type": "zellij_send", "text": "{{ text }}"}
    action.update(action_overrides)
    return {
        "name": "test_zellij",
        "description": "Send to zellij",
        "parameters": [{"name": "text", "type": "string", "required": True}],
        "action": action,
        "response": {
            "template": "Sent: {{ text }}",
            "error_template": "fail",
        },
    }


class TestZellijSendAction:
    def test_default_session_is_cc(self):
        skill = _zellij_send_skill()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            YAMLInterpreter().execute(skill, {"text": "hello"})
        argv = mock_run.call_args_list[0].args[0]
        assert argv[:4] == ["zellij", "--session", "cc", "action"]

    def test_write_chars_carries_text(self):
        skill = _zellij_send_skill()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            YAMLInterpreter().execute(skill, {"text": "你好 cc"})
        argv = mock_run.call_args_list[0].args[0]
        assert argv[4] == "write-chars"
        assert argv[5] == "你好 cc"

    def test_submit_true_sends_enter_byte_after(self):
        skill = _zellij_send_skill(submit=True)
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            YAMLInterpreter().execute(skill, {"text": "x"})
        assert mock_run.call_count == 2
        second = mock_run.call_args_list[1].args[0]
        assert second[4:] == ["write", "13"]

    def test_submit_false_no_enter_byte(self):
        skill = _zellij_send_skill(submit=False)
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            YAMLInterpreter().execute(skill, {"text": "x"})
        assert mock_run.call_count == 1

    def test_session_override_via_template(self):
        skill = _zellij_send_skill(session="{{ sess }}")
        skill["parameters"].append(
            {"name": "sess", "type": "string", "required": True}
        )
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            YAMLInterpreter().execute(skill, {"text": "x", "sess": "frontend"})
        argv = mock_run.call_args_list[0].args[0]
        assert argv[2] == "frontend"

    def test_empty_text_short_circuits(self):
        skill = _zellij_send_skill()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            out = YAMLInterpreter().execute(skill, {"text": ""})
        assert "无内容" in out
        mock_run.assert_not_called()

    def test_invalid_session_flag_injection_blocked(self):
        skill = _zellij_send_skill(session="--config evil")
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            out = YAMLInterpreter().execute(skill, {"text": "x"})
        mock_run.assert_not_called()
        assert out == "fail"

    def test_session_with_shell_metachars_blocked(self):
        skill = _zellij_send_skill(session="cc; rm -rf /")
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            out = YAMLInterpreter().execute(skill, {"text": "x"})
        mock_run.assert_not_called()
        assert out == "fail"

    def test_session_with_spaces_blocked(self):
        skill = _zellij_send_skill(session="my session")
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            out = YAMLInterpreter().execute(skill, {"text": "x"})
        mock_run.assert_not_called()

    def test_session_alphanumeric_dash_underscore_allowed(self):
        for name in ("cc", "frontend", "back-end", "my_session", "Test123"):
            skill = _zellij_send_skill(session=name)
            with patch("core.yaml_interpreter.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                YAMLInterpreter().execute(skill, {"text": "x"})
            assert mock_run.call_args_list[0].args[0][2] == name

    def test_zellij_not_found_friendly_error(self):
        skill = _zellij_send_skill()
        skill["response"].pop("error_template")
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("zellij")
            out = YAMLInterpreter().execute(skill, {"text": "x"})
        assert "zellij" in out

    def test_called_process_error_returns_error_template(self):
        import subprocess as _sp
        skill = _zellij_send_skill()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.side_effect = _sp.CalledProcessError(
                1, ["zellij"], stderr=b"no such session"
            )
            out = YAMLInterpreter().execute(skill, {"text": "x"})
        assert out == "fail"

    def test_subprocess_timeout_returns_error_template(self):
        import subprocess as _sp
        skill = _zellij_send_skill()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.side_effect = _sp.TimeoutExpired(cmd="zellij", timeout=3)
            out = YAMLInterpreter().execute(skill, {"text": "x"})
        assert out == "fail"

    def test_text_with_shell_metachars_inert_in_argv(self):
        """Text reaches argv intact but shell-inert (subprocess uses argv list)."""
        skill = _zellij_send_skill()
        evil = '"; rm -rf /; echo "'
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            YAMLInterpreter().execute(skill, {"text": evil})
        argv = mock_run.call_args_list[0].args[0]
        assert evil in argv  # passed verbatim
        # Confirm shell=False (default — never enabled)
        for call in mock_run.call_args_list:
            assert call.kwargs.get("shell", False) is False
            assert isinstance(call.args[0], list)

    def test_response_template_rendered(self):
        skill = _zellij_send_skill()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            out = YAMLInterpreter().execute(skill, {"text": "hi"})
        assert out == "Sent: hi"


# ===========================================================================
# Real cc_tell.yaml end-to-end (mocked subprocess)
# ===========================================================================


class TestCCTellYAMLSkill:
    def _load(self):
        interp = YAMLInterpreter()
        skill_path = os.path.join(
            os.path.dirname(__file__), "..", "skills", "cc_tell.yaml"
        )
        return interp, interp.load_skill(skill_path)

    def test_loads_correct_schema(self):
        interp, skill = self._load()
        td = interp.to_tool_definition(skill)
        assert td["name"] == "cc_tell"
        assert td["input_schema"]["required"] == ["text"]
        # Trigger anchors documented in description
        desc = td["description"].lower()
        for anchor in ("让 cc", "tell cc", "ask cc"):
            assert anchor.lower() in desc

    def test_default_session_cc(self):
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            interp.execute(skill, {"text": "refactor jarvis.py"})
        argv = mock_run.call_args_list[0].args[0]
        assert argv[2] == "cc"
        assert "refactor jarvis.py" in argv

    def test_auto_submits_after_text(self):
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            interp.execute(skill, {"text": "x"})
        assert mock_run.call_count == 2
        assert mock_run.call_args_list[1].args[0][-2:] == ["write", "13"]

    def test_response_includes_text(self):
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            out = interp.execute(skill, {"text": "hello cc"})
        assert "hello cc" in out
        assert "已发给" in out

    def test_long_text_truncated_in_response(self):
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            out = interp.execute(skill, {"text": "a" * 100})
        assert "..." in out

    def test_explicit_session_override(self):
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            out = interp.execute(skill, {"text": "x", "session": "frontend"})
        argv = mock_run.call_args_list[0].args[0]
        assert argv[2] == "frontend"
        assert "frontend" in out

    def test_failure_returns_friendly_error_template(self):
        import subprocess as _sp
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.side_effect = _sp.CalledProcessError(
                1, ["zellij"], stderr=b"no such session"
            )
            out = interp.execute(skill, {"text": "x"})
        assert "zellij list-sessions" in out


# ===========================================================================
# zellij_send keys param — Phase 1A: special-key whitelist
# ===========================================================================


class TestZellijKeysParam:
    def _skill(self, **action_overrides):
        action = {"type": "zellij_send"}
        action.update(action_overrides)
        return {
            "name": "test_keys",
            "description": "keys test",
            "parameters": [],
            "action": action,
            "response": {
                "template": "ok",
                "error_template": "fail",
            },
        }

    def test_ctrl_c_writes_byte_3(self):
        skill = self._skill(keys=["c-c"])
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            YAMLInterpreter().execute(skill, {})
        assert mock_run.call_count == 1
        argv = mock_run.call_args_list[0].args[0]
        # zellij --session cc action write 3
        assert argv[0] == "zellij"
        assert argv[2] == "cc"
        assert argv[3:5] == ["action", "write"]
        assert argv[5] == "3"

    def test_arrow_up_writes_three_bytes_in_one_call(self):
        skill = self._skill(keys=["up"])
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            YAMLInterpreter().execute(skill, {})
        argv = mock_run.call_args_list[0].args[0]
        # write 27 91 65 — multi-byte ANSI sequence
        assert argv[4:] == ["write", "27", "91", "65"]

    def test_keys_only_no_text_no_submit_allowed(self):
        skill = self._skill(keys=["esc"])
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            out = YAMLInterpreter().execute(skill, {})
        # Single zellij call (one key); no "无内容" error
        assert mock_run.call_count == 1
        assert out == "ok"

    def test_empty_text_empty_keys_short_circuits(self):
        skill = self._skill()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            out = YAMLInterpreter().execute(skill, {})
        assert "无内容" in out
        mock_run.assert_not_called()

    def test_keys_then_text_then_submit_order(self):
        skill = self._skill(keys=["esc"], text="hello", submit=True)
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            YAMLInterpreter().execute(skill, {})
        assert mock_run.call_count == 3
        # Call 0: write 27 (esc)
        assert mock_run.call_args_list[0].args[0][4:] == ["write", "27"]
        # Call 1: write-chars hello
        assert mock_run.call_args_list[1].args[0][4:] == ["write-chars", "hello"]
        # Call 2: write 13 (Enter)
        assert mock_run.call_args_list[2].args[0][4:] == ["write", "13"]

    def test_unknown_key_blocked_before_subprocess(self):
        skill = self._skill(keys=["c-q"])
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            out = YAMLInterpreter().execute(skill, {})
        mock_run.assert_not_called()
        assert out == "fail"

    def test_unknown_key_no_error_template_shows_whitelist(self):
        skill = self._skill(keys=["fookey"])
        skill["response"].pop("error_template")
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            out = YAMLInterpreter().execute(skill, {})
        mock_run.assert_not_called()
        assert "fookey" in out
        assert "c-c" in out  # whitelist mentioned in error

    def test_key_case_normalized(self):
        skill = self._skill(keys=["C-C"])
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            YAMLInterpreter().execute(skill, {})
        # uppercase user input still maps via lower()
        assert mock_run.call_args_list[0].args[0][5] == "3"

    def test_multiple_keys_sent_as_separate_calls(self):
        skill = self._skill(keys=["esc", "c-c"])
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            YAMLInterpreter().execute(skill, {})
        assert mock_run.call_count == 2
        assert mock_run.call_args_list[0].args[0][4:] == ["write", "27"]
        assert mock_run.call_args_list[1].args[0][4:] == ["write", "3"]


# ===========================================================================
# Real cc_interrupt.yaml end-to-end
# ===========================================================================


class TestCCInterruptYAMLSkill:
    def _load(self):
        interp = YAMLInterpreter()
        skill_path = os.path.join(
            os.path.dirname(__file__), "..", "skills", "cc_interrupt.yaml"
        )
        return interp, interp.load_skill(skill_path)

    def test_loads_correct_schema(self):
        interp, skill = self._load()
        td = interp.to_tool_definition(skill)
        assert td["name"] == "cc_interrupt"
        # text is NOT in params (key-only skill)
        assert "text" not in td["input_schema"]["properties"]
        # session is optional
        assert td["input_schema"]["required"] == []
        desc = td["description"].lower()
        for anchor in ("停 cc", "interrupt cc", "abort cc"):
            assert anchor.lower() in desc

    def test_sends_ctrl_c_to_cc_session(self):
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            out = interp.execute(skill, {})
        assert mock_run.call_count == 1
        argv = mock_run.call_args_list[0].args[0]
        assert argv == ["zellij", "--session", "cc", "action", "write", "3"]
        assert "已打断" in out


# ===========================================================================
# Real cc_approve.yaml end-to-end
# ===========================================================================


class TestCCApproveYAMLSkill:
    def _load(self):
        interp = YAMLInterpreter()
        skill_path = os.path.join(
            os.path.dirname(__file__), "..", "skills", "cc_approve.yaml"
        )
        return interp, interp.load_skill(skill_path)

    def test_writes_y_then_enter(self):
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            out = interp.execute(skill, {})
        assert mock_run.call_count == 2
        assert mock_run.call_args_list[0].args[0][4:] == ["write-chars", "y"]
        assert mock_run.call_args_list[1].args[0][4:] == ["write", "13"]
        assert "已批准" in out


# ===========================================================================
# Real cc_deny.yaml end-to-end
# ===========================================================================


class TestCCDenyYAMLSkill:
    def _load(self):
        interp = YAMLInterpreter()
        skill_path = os.path.join(
            os.path.dirname(__file__), "..", "skills", "cc_deny.yaml"
        )
        return interp, interp.load_skill(skill_path)

    def test_writes_n_then_enter(self):
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            out = interp.execute(skill, {})
        assert mock_run.call_count == 2
        assert mock_run.call_args_list[0].args[0][4:] == ["write-chars", "n"]
        assert mock_run.call_args_list[1].args[0][4:] == ["write", "13"]
        assert "已拒绝" in out


# ===========================================================================
# Real cc_slash.yaml end-to-end
# ===========================================================================


class TestCCSlashYAMLSkill:
    def _load(self):
        interp = YAMLInterpreter()
        skill_path = os.path.join(
            os.path.dirname(__file__), "..", "skills", "cc_slash.yaml"
        )
        return interp, interp.load_skill(skill_path)

    def test_loads_correct_schema(self):
        interp, skill = self._load()
        td = interp.to_tool_definition(skill)
        assert td["name"] == "cc_slash"
        assert td["input_schema"]["required"] == ["command"]
        assert "command" in td["input_schema"]["properties"]
        assert "args" in td["input_schema"]["properties"]

    def test_simple_commit_command(self):
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            out = interp.execute(skill, {"command": "commit"})
        assert mock_run.call_count == 2
        # write-chars "/commit"
        assert mock_run.call_args_list[0].args[0][4:] == ["write-chars", "/commit"]
        # write 13 (Enter)
        assert mock_run.call_args_list[1].args[0][4:] == ["write", "13"]
        assert "/commit" in out

    def test_command_with_args(self):
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            out = interp.execute(skill, {"command": "model", "args": "opus"})
        assert mock_run.call_args_list[0].args[0][4:] == ["write-chars", "/model opus"]
        assert "/model opus" in out

    def test_no_args_no_trailing_space(self):
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            interp.execute(skill, {"command": "compact"})
        # No trailing space when args empty (argv[4]=write-chars, argv[5]=text)
        assert mock_run.call_args_list[0].args[0][5] == "/compact"


# ---------------------------------------------------------------------------
# cc_read_state action + cc_show YAML skill (cc_bridge Phase 1B)
# ---------------------------------------------------------------------------


def _cc_read_state_skill(**action_overrides) -> dict:
    """Minimal skill driving the cc_read_state action."""
    action = {"type": "cc_read_state"}
    action.update(action_overrides)
    return {
        "name": "test_cc_read_state",
        "description": "Read cc state",
        "parameters": [],
        "action": action,
        "response": {"template": "{{ result }}", "error_template": "fail"},
    }


def _list_panes_payload(panes: list[dict]) -> str:
    """Render a list-panes JSON payload like zellij would emit."""
    return json.dumps(panes)


_DEFAULT_PANE = {
    "id": 1,
    "is_plugin": False,
    "is_focused": True,
    "title": "Pane #2",
    "pane_command": "claude",
    "pane_cwd": "/tmp/proj",
}

_PLUGIN_PANE = {
    "id": 0,
    "is_plugin": True,
    "is_focused": False,
    "title": "Pane #2",   # same title as default — must be skipped (is_plugin)
    "pane_cwd": None,
}


def _subprocess_factory(panes_json: str, dump_text: str = "$ ls\nfile1\nfile2\n"):
    """Return a fake subprocess.run that dispatches by argv inspection."""

    def _fake_run(argv, **kwargs):
        if "list-panes" in argv:
            return MagicMock(returncode=0, stdout=panes_json, stderr="")
        if "dump-screen" in argv:
            return MagicMock(returncode=0, stdout=dump_text, stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    return _fake_run


class TestCCReadStateResolver:
    def test_resolver_matches_title_returns_terminal_id_and_cwd(self):
        skill = _cc_read_state_skill()
        panes = _list_panes_payload([_PLUGIN_PANE, _DEFAULT_PANE])
        with patch(
            "core.yaml_interpreter.subprocess.run",
            side_effect=_subprocess_factory(panes),
        ):
            with patch(
                "core.yaml_interpreter.cc_jsonl_reader.find_recent_jsonls",
                return_value=[],
            ):
                out = YAMLInterpreter().execute(skill, {})
        assert "终端状态" in out
        assert "file1" in out

    def test_resolver_skips_plugin_panes_with_same_title(self):
        skill = _cc_read_state_skill()
        # Only the plugin pane has the matching title — should fail.
        panes = _list_panes_payload([_PLUGIN_PANE])
        with patch(
            "core.yaml_interpreter.subprocess.run",
            side_effect=_subprocess_factory(panes),
        ):
            out = YAMLInterpreter().execute(skill, {})
        assert out == "fail"

    def test_resolver_returns_error_template_when_title_missing(self):
        skill = _cc_read_state_skill(pane_title="Pane #99")
        panes = _list_panes_payload([_DEFAULT_PANE])
        with patch(
            "core.yaml_interpreter.subprocess.run",
            side_effect=_subprocess_factory(panes),
        ):
            out = YAMLInterpreter().execute(skill, {})
        assert out == "fail"

    def test_resolver_passes_session_to_subprocess(self):
        skill = _cc_read_state_skill(session="frontend")
        panes = _list_panes_payload([_DEFAULT_PANE])
        captured: list[list] = []

        def _capture(argv, **kwargs):
            captured.append(argv)
            return _subprocess_factory(panes)(argv, **kwargs)

        with patch("core.yaml_interpreter.subprocess.run", side_effect=_capture):
            with patch(
                "core.yaml_interpreter.cc_jsonl_reader.find_recent_jsonls",
                return_value=[],
            ):
                YAMLInterpreter().execute(skill, {})
        list_panes_argv = next(a for a in captured if "list-panes" in a)
        assert list_panes_argv[:3] == ["zellij", "--session", "frontend"]


class TestCCReadStateExecution:
    def test_invalid_session_short_circuits_before_subprocess(self):
        skill = _cc_read_state_skill(session="--config evil")
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            out = YAMLInterpreter().execute(skill, {})
        mock_run.assert_not_called()
        assert out == "fail"

    def test_candidates_rendered_with_user_assistant_and_tool_blocks(self, tmp_path):
        """Candidate format must surface user prompt + assistant text + tools so LLM can fingerprint."""
        from core.cc_jsonl_reader import CCCandidate, ToolCall

        skill = _cc_read_state_skill()
        panes = _list_panes_payload([_DEFAULT_PANE])
        cand = CCCandidate(
            jsonl_path=tmp_path / "abc12345-rest.jsonl",
            session_id="abc12345-rest",
            mtime=time.time() - 30,
            last_user_prompt="帮我修 router 的 bug",
            last_assistant_text="Let me grep for that",
            recent_tool_calls=[ToolCall(name="Grep", input_summary="router")],
            empty=False,
        )
        with patch(
            "core.yaml_interpreter.subprocess.run",
            side_effect=_subprocess_factory(panes),
        ):
            with patch(
                "core.yaml_interpreter.cc_jsonl_reader.find_recent_jsonls",
                return_value=[cand.jsonl_path],
            ):
                with patch(
                    "core.yaml_interpreter.cc_jsonl_reader.read_recent_exchange",
                    return_value=cand,
                ):
                    out = YAMLInterpreter().execute(skill, {})
        # User prompt and assistant text must appear in the candidate block
        assert "帮我修 router 的 bug" in out
        assert "Let me grep for that" in out
        # Tool call rendered with name(input_summary)
        assert "Grep(router)" in out
        # Session id prefix shown (first 8 chars)
        assert "abc12345" in out
        # Matching instructions present
        assert "匹配指引" in out

    def test_no_candidates_falls_back_to_viewport_only(self):
        skill = _cc_read_state_skill()
        panes = _list_panes_payload([_DEFAULT_PANE])
        with patch(
            "core.yaml_interpreter.subprocess.run",
            side_effect=_subprocess_factory(panes, dump_text="some terminal output\n"),
        ):
            with patch(
                "core.yaml_interpreter.cc_jsonl_reader.find_recent_jsonls",
                return_value=[],
            ):
                out = YAMLInterpreter().execute(skill, {})
        # Viewport must still appear
        assert "some terminal output" in out
        # And the no-candidates fallback message
        assert "没找到 cc 对话日志候选" in out
        # Matching instructions still appear (LLM told to use viewport alone)
        assert "匹配指引" in out

    def test_top_n_candidates_passed_to_jsonl_reader(self, tmp_path):
        """cc_show should request 3 candidates from find_recent_jsonls."""
        skill = _cc_read_state_skill()
        panes = _list_panes_payload([_DEFAULT_PANE])
        with patch(
            "core.yaml_interpreter.subprocess.run",
            side_effect=_subprocess_factory(panes),
        ):
            with patch(
                "core.yaml_interpreter.cc_jsonl_reader.find_recent_jsonls",
                return_value=[],
            ) as mock_find:
                YAMLInterpreter().execute(skill, {})
        mock_find.assert_called_once()
        # Second positional or keyword arg should be n=3
        call_kwargs = mock_find.call_args.kwargs
        call_args = mock_find.call_args.args
        n = call_kwargs.get("n", call_args[1] if len(call_args) > 1 else None)
        assert n == 3

    def test_empty_candidates_filtered_from_output(self, tmp_path):
        """Candidates with empty=True should be skipped — they have no fingerprint to match."""
        from core.cc_jsonl_reader import CCCandidate

        skill = _cc_read_state_skill()
        panes = _list_panes_payload([_DEFAULT_PANE])
        empty_cand = CCCandidate(
            jsonl_path=tmp_path / "empty.jsonl",
            session_id="empty-session",
            mtime=time.time(),
            empty=True,
        )
        good_cand = CCCandidate(
            jsonl_path=tmp_path / "good.jsonl",
            session_id="good-session",
            mtime=time.time() - 60,
            last_assistant_text="real content",
            empty=False,
        )
        with patch(
            "core.yaml_interpreter.subprocess.run",
            side_effect=_subprocess_factory(panes),
        ):
            with patch(
                "core.yaml_interpreter.cc_jsonl_reader.find_recent_jsonls",
                return_value=[empty_cand.jsonl_path, good_cand.jsonl_path],
            ):
                with patch(
                    "core.yaml_interpreter.cc_jsonl_reader.read_recent_exchange",
                    side_effect=[empty_cand, good_cand],
                ):
                    out = YAMLInterpreter().execute(skill, {})
        assert "good-session"[:8] in out
        assert "empty-se"  not in out or "empty-session" not in out  # not surfaced

    def test_oversized_dump_truncated_to_8kb(self):
        skill = _cc_read_state_skill()
        panes = _list_panes_payload([_DEFAULT_PANE])
        big = "x" * 20000  # 20KB > 8KB cap
        with patch(
            "core.yaml_interpreter.subprocess.run",
            side_effect=_subprocess_factory(panes, dump_text=big),
        ):
            with patch(
                "core.yaml_interpreter.cc_jsonl_reader.find_recent_jsonls",
                return_value=[],
            ):
                out = YAMLInterpreter().execute(skill, {})
        # Cap is on the full dump, but ui_tail is just the last 25 lines so it's a
        # single trailing block. Truncation marker only appears if >8KB.
        assert "[viewport truncated]" in out

    def test_dump_screen_subprocess_failure_returns_error_template(self):
        skill = _cc_read_state_skill()
        panes = _list_panes_payload([_DEFAULT_PANE])

        def _fake_run(argv, **kwargs):
            if "list-panes" in argv:
                return MagicMock(returncode=0, stdout=panes, stderr="")
            if "dump-screen" in argv:
                from subprocess import CalledProcessError
                raise CalledProcessError(1, argv, output="", stderr="dump failed")
            return MagicMock(returncode=0)

        with patch("core.yaml_interpreter.subprocess.run", side_effect=_fake_run):
            out = YAMLInterpreter().execute(skill, {})
        # error_template is the canonical fallback string.
        assert out == "fail" or "dump failed" in out

    def test_zellij_not_installed_returns_error_template(self):
        skill = _cc_read_state_skill()
        with patch(
            "core.yaml_interpreter.subprocess.run",
            side_effect=FileNotFoundError("zellij"),
        ):
            out = YAMLInterpreter().execute(skill, {})
        assert out == "fail"

    def test_pane_id_format_is_terminal_underscore_n(self):
        skill = _cc_read_state_skill()
        panes = _list_panes_payload([_DEFAULT_PANE])
        captured: list[list] = []

        def _capture(argv, **kwargs):
            captured.append(argv)
            if "list-panes" in argv:
                return MagicMock(returncode=0, stdout=panes, stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("core.yaml_interpreter.subprocess.run", side_effect=_capture):
            with patch(
                "core.yaml_interpreter.cc_jsonl_reader.find_recent_jsonls",
                return_value=[],
            ):
                YAMLInterpreter().execute(skill, {})
        dump_argv = next(a for a in captured if "dump-screen" in a)
        assert "--pane-id" in dump_argv
        idx = dump_argv.index("--pane-id")
        assert dump_argv[idx + 1] == "terminal_1"


class TestCCShowYAMLSkill:
    def test_cc_show_yaml_loads_and_registers(self):
        interp = YAMLInterpreter()
        skill = interp.load_skill("skills/cc_show.yaml")
        assert skill["name"] == "cc_show"
        tool = interp.to_tool_definition(skill)
        assert tool["name"] == "cc_show"
        # Both params optional — required list should be empty.
        assert tool["input_schema"]["required"] == []

    def test_cc_show_default_params_target_pane_2_in_session_cc(self):
        interp = YAMLInterpreter()
        skill = interp.load_skill("skills/cc_show.yaml")
        panes = _list_panes_payload([_DEFAULT_PANE])
        captured: list[list] = []

        def _capture(argv, **kwargs):
            captured.append(argv)
            if "list-panes" in argv:
                return MagicMock(returncode=0, stdout=panes, stderr="")
            return MagicMock(returncode=0, stdout="$ idle\n", stderr="")

        with patch("core.yaml_interpreter.subprocess.run", side_effect=_capture):
            with patch(
                "core.yaml_interpreter.cc_jsonl_reader.find_recent_jsonls",
                return_value=[],
            ):
                interp.execute(skill, {})
        list_argv = next(a for a in captured if "list-panes" in a)
        assert list_argv[:3] == ["zellij", "--session", "cc"]

    def test_cc_show_marked_read_only(self):
        interp = YAMLInterpreter()
        skill = interp.load_skill("skills/cc_show.yaml")
        assert skill["annotations"]["read_only"] is True
        assert skill["annotations"]["destructive"] is False


# ===========================================================================
# aerospace_op action — mocked subprocess
# ===========================================================================


def _aerospace_skill(registry: dict, display_aliases: dict | None = None) -> dict:
    """Build a minimal aerospace_op skill for unit tests."""
    return {
        "name": "mac_gui_test",
        "description": "test mac_gui",
        "parameters": [
            {"name": "action_id", "type": "string", "required": True},
            {"name": "app", "type": "string", "required": False, "default": ""},
            {"name": "display", "type": "string", "required": False, "default": ""},
        ],
        "action": {
            "type": "aerospace_op",
            "registry": registry,
            "display_aliases": display_aliases or {},
        },
        "response": {
            "template": "{{ result }}",
            "error_template": "MAC_GUI_ERR",
        },
    }


class TestAeroSpaceOpDispatch:
    def test_unknown_action_id_returns_error_template(self):
        skill = _aerospace_skill({"focus_app": {"steps": [], "response": "ok"}})
        out = YAMLInterpreter().execute(skill, {"action_id": "no_such"})
        assert out == "MAC_GUI_ERR"

    def test_unknown_action_id_no_error_template_lists_known(self):
        skill = _aerospace_skill({"focus_app": {"steps": [], "response": "ok"}})
        skill["response"].pop("error_template")
        out = YAMLInterpreter().execute(skill, {"action_id": "no_such"})
        assert "no_such" in out
        assert "focus_app" in out

    def test_empty_action_id_returns_error(self):
        skill = _aerospace_skill({"focus_app": {"steps": [], "response": "ok"}})
        out = YAMLInterpreter().execute(skill, {"action_id": ""})
        assert out == "MAC_GUI_ERR"

    def test_empty_steps_renders_response(self):
        skill = _aerospace_skill({"x": {"steps": [], "response": "OK-{{ app }}"}})
        out = YAMLInterpreter().execute(skill, {"action_id": "x", "app": "Cursor"})
        assert out == "OK-Cursor"


class TestAeroSpaceOpShellStep:
    def test_shell_step_renders_each_argv_part(self):
        registry = {
            "x": {
                "steps": [{"type": "shell", "cmd": ["echo", "{{ app }}"]}],
                "response": "ok",
            }
        }
        skill = _aerospace_skill(registry)
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            YAMLInterpreter().execute(skill, {"action_id": "x", "app": "抖音"})
        argv = mock_run.call_args_list[0].args[0]
        assert argv == ["echo", "抖音"]

    def test_shell_step_uses_argv_list_no_shell(self):
        registry = {
            "x": {
                "steps": [{"type": "shell", "cmd": ["open", "-a", "{{ app }}"]}],
                "response": "ok",
            }
        }
        skill = _aerospace_skill(registry)
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            YAMLInterpreter().execute(skill, {"action_id": "x", "app": "Discord"})
        kwargs = mock_run.call_args_list[0].kwargs
        # shell mode never enabled
        assert kwargs.get("shell") in (False, None)
        assert kwargs.get("check") is True

    def test_shell_step_user_metachars_inert_in_argv(self):
        registry = {
            "x": {
                "steps": [{"type": "shell", "cmd": ["open", "-a", "{{ app }}"]}],
                "response": "ok",
            }
        }
        skill = _aerospace_skill(registry)
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            evil = '"; rm -rf /; echo "'
            YAMLInterpreter().execute(skill, {"action_id": "x", "app": evil})
        argv = mock_run.call_args_list[0].args[0]
        # whole evil string lives in one argv slot, never split or interpreted
        assert argv == ["open", "-a", evil]

    def test_shell_step_capture_var_passes_to_next_step(self):
        registry = {
            "x": {
                "steps": [
                    {"type": "shell", "cmd": ["echo", "42"], "capture_var": "wid"},
                    {"type": "shell", "cmd": ["touch", "{{ wid }}"]},
                ],
                "response": "wid={{ wid }}",
            }
        }
        skill = _aerospace_skill(registry)
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(stdout="42\n", returncode=0),
                MagicMock(stdout="", returncode=0),
            ]
            out = YAMLInterpreter().execute(skill, {"action_id": "x"})
        assert out == "wid=42"
        assert mock_run.call_args_list[1].args[0] == ["touch", "42"]

    def test_shell_step_called_process_error_returns_error_template(self):
        import subprocess
        registry = {
            "x": {"steps": [{"type": "shell", "cmd": ["false"]}], "response": "ok"}
        }
        skill = _aerospace_skill(registry)
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                1, ["false"], stderr=b"bad"
            )
            assert YAMLInterpreter().execute(skill, {"action_id": "x"}) == "MAC_GUI_ERR"

    def test_shell_step_file_not_found_returns_error_template(self):
        registry = {
            "x": {"steps": [{"type": "shell", "cmd": ["aerospace"]}], "response": "ok"}
        }
        skill = _aerospace_skill(registry)
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("aerospace")
            assert YAMLInterpreter().execute(skill, {"action_id": "x"}) == "MAC_GUI_ERR"

    def test_shell_step_timeout_returns_error_template(self):
        import subprocess
        registry = {
            "x": {"steps": [{"type": "shell", "cmd": ["sleep", "100"]}], "response": "ok"}
        }
        skill = _aerospace_skill(registry)
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd=["sleep"], timeout=5)
            assert YAMLInterpreter().execute(skill, {"action_id": "x"}) == "MAC_GUI_ERR"

    def test_shell_step_missing_cmd_returns_error_template(self):
        registry = {"x": {"steps": [{"type": "shell"}], "response": "ok"}}
        skill = _aerospace_skill(registry)
        assert YAMLInterpreter().execute(skill, {"action_id": "x"}) == "MAC_GUI_ERR"

    def test_shell_step_empty_cmd_list_returns_error_template(self):
        registry = {"x": {"steps": [{"type": "shell", "cmd": []}], "response": "ok"}}
        skill = _aerospace_skill(registry)
        assert YAMLInterpreter().execute(skill, {"action_id": "x"}) == "MAC_GUI_ERR"


class TestAeroSpaceOpAppleScriptStep:
    def test_applescript_uses_argv_binding(self):
        script_body = (
            "on run argv\n"
            "  tell application (item 1 of argv) to activate\n"
            "end run"
        )
        registry = {
            "x": {
                "steps": [
                    {"type": "applescript", "script": script_body, "argv": ["{{ app }}"]}
                ],
                "response": "ok",
            }
        }
        skill = _aerospace_skill(registry)
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            YAMLInterpreter().execute(skill, {"action_id": "x", "app": "Cursor"})
        argv = mock_run.call_args_list[0].args[0]
        assert argv[:3] == ["osascript", "-e", script_body]
        assert argv[3:] == ["Cursor"]

    def test_applescript_script_body_is_literal_no_jinja_render(self):
        # Critical injection guard: any {{ }} inside the script body must
        # be treated as literal AppleScript text, never rendered.
        script_body = (
            "on run argv\n"
            "  return (item 1 of argv)\n"
            "end run -- {{ never_rendered }}"
        )
        registry = {
            "x": {
                "steps": [
                    {"type": "applescript", "script": script_body, "argv": ["safe"]}
                ],
                "response": "ok",
            }
        }
        skill = _aerospace_skill(registry)
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            # Try to leak a value through Jinja — must not be substituted
            YAMLInterpreter().execute(
                skill, {"action_id": "x", "never_rendered": "LEAKED"}
            )
        argv = mock_run.call_args_list[0].args[0]
        assert argv[2] == script_body
        assert "LEAKED" not in argv[2]

    def test_applescript_user_input_only_via_argv(self):
        script_body = (
            "on run argv\n"
            "  tell application (item 1 of argv) to activate\n"
            "end run"
        )
        registry = {
            "x": {
                "steps": [
                    {"type": "applescript", "script": script_body, "argv": ["{{ app }}"]}
                ],
                "response": "ok",
            }
        }
        skill = _aerospace_skill(registry)
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            evil = '"; do shell script "rm -rf /"; tell app "X'
            YAMLInterpreter().execute(skill, {"action_id": "x", "app": evil})
        argv = mock_run.call_args_list[0].args[0]
        # script body unchanged; evil flows only into the argv positional
        assert argv[2] == script_body
        assert argv[3] == evil
        # evil cannot be a -e fragment because it lives at position 3+
        assert argv.index("-e") == 1
        assert "-e" not in argv[3:]

    def test_applescript_missing_script_returns_error_template(self):
        registry = {"x": {"steps": [{"type": "applescript"}], "response": "ok"}}
        skill = _aerospace_skill(registry)
        assert YAMLInterpreter().execute(skill, {"action_id": "x"}) == "MAC_GUI_ERR"

    def test_applescript_empty_argv_works(self):
        # Some scripts don't need argv at all (e.g. "set volume" with hardcoded
        # value). Empty argv is valid.
        script_body = "tell application \"System Events\" to log \"hi\""
        registry = {
            "x": {
                "steps": [{"type": "applescript", "script": script_body}],
                "response": "ok",
            }
        }
        skill = _aerospace_skill(registry)
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            YAMLInterpreter().execute(skill, {"action_id": "x"})
        argv = mock_run.call_args_list[0].args[0]
        assert argv == ["osascript", "-e", script_body]


class TestAeroSpaceOpPollWindow:
    def test_poll_window_finds_match_and_captures_wid(self):
        registry = {
            "x": {
                "steps": [
                    {
                        "type": "poll_window",
                        "match_app": "{{ app }}",
                        "capture_var": "wid",
                        "timeout_s": 1.0,
                        "interval_s": 0.01,
                    },
                    {"type": "shell", "cmd": ["echo", "{{ wid }}"]},
                ],
                "response": "wid={{ wid }}",
            }
        }
        skill = _aerospace_skill(registry)
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(stdout="42\t抖音\tdouyin\n", returncode=0),  # poll
                MagicMock(stdout="", returncode=0),                    # echo
            ]
            out = YAMLInterpreter().execute(skill, {"action_id": "x", "app": "抖音"})
        assert out == "wid=42"
        assert mock_run.call_args_list[1].args[0] == ["echo", "42"]

    def test_poll_window_skips_non_matching_apps(self):
        registry = {
            "x": {
                "steps": [
                    {
                        "type": "poll_window",
                        "match_app": "{{ app }}",
                        "capture_var": "wid",
                        "timeout_s": 1.0,
                        "interval_s": 0.01,
                    }
                ],
                "response": "wid={{ wid }}",
            }
        }
        skill = _aerospace_skill(registry)
        rows = "1\tOther App\ttitle\n99\t抖音\tdouyin\n"
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=rows, returncode=0)
            out = YAMLInterpreter().execute(skill, {"action_id": "x", "app": "抖音"})
        assert out == "wid=99"

    def test_poll_window_respects_title_substring(self):
        registry = {
            "x": {
                "steps": [
                    {
                        "type": "poll_window",
                        "match_app": "Google Chrome",
                        "match_title_contains": "{{ needle }}",
                        "capture_var": "wid",
                        "timeout_s": 1.0,
                        "interval_s": 0.01,
                    }
                ],
                "response": "wid={{ wid }}",
            }
        }
        skill = _aerospace_skill(registry)
        rows = (
            "1\tGoogle Chrome\tFoo - Google Chrome\n"
            "2\tGoogle Chrome\t抖音-记录美好生活\n"
        )
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=rows, returncode=0)
            out = YAMLInterpreter().execute(
                skill, {"action_id": "x", "needle": "抖音"}
            )
        assert out == "wid=2"

    def test_poll_window_times_out_returns_error_template(self):
        registry = {
            "x": {
                "steps": [
                    {
                        "type": "poll_window",
                        "match_app": "Nonexistent",
                        "capture_var": "wid",
                        "timeout_s": 0.05,
                        "interval_s": 0.01,
                    }
                ],
                "response": "ok",
            }
        }
        skill = _aerospace_skill(registry)
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            assert YAMLInterpreter().execute(skill, {"action_id": "x"}) == "MAC_GUI_ERR"

    def test_poll_window_handles_aerospace_called_process_error(self):
        # When AeroSpace errors transiently, poll loop swallows the error and
        # continues until its own timeout. Verifies we don't crash on a
        # CalledProcessError mid-loop.
        import subprocess
        registry = {
            "x": {
                "steps": [
                    {
                        "type": "poll_window",
                        "match_app": "Whatever",
                        "capture_var": "wid",
                        "timeout_s": 0.05,
                        "interval_s": 0.01,
                    }
                ],
                "response": "ok",
            }
        }
        skill = _aerospace_skill(registry)
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                1, ["aerospace"], stderr=b"server unavailable"
            )
            # eventually times out — _PollTimeout, not CalledProcessError
            assert YAMLInterpreter().execute(skill, {"action_id": "x"}) == "MAC_GUI_ERR"

    def test_poll_window_file_not_found_propagates(self):
        # AeroSpace not installed: poll_window does NOT swallow this.
        registry = {
            "x": {
                "steps": [
                    {
                        "type": "poll_window",
                        "match_app": "X",
                        "capture_var": "wid",
                        "timeout_s": 1.0,
                        "interval_s": 0.01,
                    }
                ],
                "response": "ok",
            }
        }
        skill = _aerospace_skill(registry)
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("aerospace")
            assert YAMLInterpreter().execute(skill, {"action_id": "x"}) == "MAC_GUI_ERR"

    def test_poll_window_missing_capture_var_returns_error(self):
        registry = {
            "x": {
                "steps": [{"type": "poll_window", "match_app": "X"}],
                "response": "ok",
            }
        }
        skill = _aerospace_skill(registry)
        assert YAMLInterpreter().execute(skill, {"action_id": "x"}) == "MAC_GUI_ERR"

    def test_poll_window_missing_match_app_returns_error(self):
        registry = {
            "x": {
                "steps": [{"type": "poll_window", "capture_var": "wid"}],
                "response": "ok",
            }
        }
        skill = _aerospace_skill(registry)
        assert YAMLInterpreter().execute(skill, {"action_id": "x"}) == "MAC_GUI_ERR"

    def test_poll_window_uses_aerospace_format_flag(self):
        # Sanity: the subprocess argv must include --format with the
        # tab-separated layout the parser expects.
        from core.yaml_interpreter import _AEROSPACE_LIST_FORMAT
        registry = {
            "x": {
                "steps": [
                    {
                        "type": "poll_window",
                        "match_app": "X",
                        "capture_var": "wid",
                        "timeout_s": 0.05,
                        "interval_s": 0.01,
                    }
                ],
                "response": "ok",
            }
        }
        skill = _aerospace_skill(registry)
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            YAMLInterpreter().execute(skill, {"action_id": "x"})
        first = mock_run.call_args_list[0].args[0]
        assert first[:3] == ["aerospace", "list-windows", "--all"]
        assert "--format" in first
        assert _AEROSPACE_LIST_FORMAT in first


class TestAeroSpaceOpDisplayAlias:
    def test_alias_resolves_before_step_render(self):
        registry = {
            "x": {
                "steps": [{"type": "shell", "cmd": ["echo", "{{ display }}"]}],
                "response": "ok",
            }
        }
        skill = _aerospace_skill(
            registry, display_aliases={"副": "BenQ", "主": "Built-in"}
        )
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            YAMLInterpreter().execute(skill, {"action_id": "x", "display": "副"})
        assert mock_run.call_args_list[0].args[0] == ["echo", "BenQ"]

    def test_pattern_passes_through_when_no_alias(self):
        registry = {
            "x": {
                "steps": [{"type": "shell", "cmd": ["echo", "{{ display }}"]}],
                "response": "ok",
            }
        }
        skill = _aerospace_skill(registry, display_aliases={"副": "BenQ"})
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            YAMLInterpreter().execute(skill, {"action_id": "x", "display": "next"})
        assert mock_run.call_args_list[0].args[0] == ["echo", "next"]

    def test_no_aliases_leaves_display_untouched(self):
        registry = {
            "x": {
                "steps": [{"type": "shell", "cmd": ["echo", "{{ display }}"]}],
                "response": "ok",
            }
        }
        skill = _aerospace_skill(registry)
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            YAMLInterpreter().execute(skill, {"action_id": "x", "display": "BenQ"})
        assert mock_run.call_args_list[0].args[0] == ["echo", "BenQ"]


class TestAeroSpaceOpStepValidation:
    def test_unsupported_step_type_returns_error(self):
        registry = {"x": {"steps": [{"type": "telepath"}], "response": "ok"}}
        skill = _aerospace_skill(registry)
        assert YAMLInterpreter().execute(skill, {"action_id": "x"}) == "MAC_GUI_ERR"

    def test_step_without_type_returns_error(self):
        registry = {"x": {"steps": [{"foo": "bar"}], "response": "ok"}}
        skill = _aerospace_skill(registry)
        assert YAMLInterpreter().execute(skill, {"action_id": "x"}) == "MAC_GUI_ERR"


# ===========================================================================
# Real skills/mac_gui.yaml end-to-end (mocked subprocess)
# ===========================================================================


class TestMacGuiYAMLSkill:
    def _load(self):
        interp = YAMLInterpreter()
        skill_path = os.path.join(
            os.path.dirname(__file__), "..", "skills", "mac_gui.yaml"
        )
        return interp, interp.load_skill(skill_path)

    def test_loads_correct_schema(self):
        interp, skill = self._load()
        td = interp.to_tool_definition(skill)
        assert td["name"] == "mac_gui"
        props = td["input_schema"]["properties"]
        assert set(props.keys()) == {
            "action_id", "app", "display",
            "url", "title_hint", "workspace", "level",
        }
        assert td["input_schema"]["required"] == ["action_id"]

    def test_registry_has_nineteen_actions(self):
        _, skill = self._load()
        registry = skill["action"]["registry"]
        assert set(registry.keys()) == {
            # launch & open
            "launch_app", "launch_app_on_display",
            "open_url", "open_url_on_display",
            # window placement
            "move_app_to_display", "move_focused_to_display", "move_to_workspace",
            # focus
            "focus_app", "focus_monitor", "focus_back_and_forth",
            # workspace
            "workspace_switch",
            # window state (resize_focused removed — AeroSpace issue #9:
            # resize doesn't support floating windows, which we use exclusively)
            "fullscreen_focused", "minimize_focused", "close_focused",
            "close_all_but_current",
            # system
            "set_volume", "mute_toggle", "lock_screen", "screenshot",
        }

    def test_display_aliases_match_expected(self):
        _, skill = self._load()
        aliases = skill["action"]["display_aliases"]
        assert aliases["副"] == "BenQ"
        assert aliases["主"] == "Built-in"
        assert aliases["外接"] == "BenQ"

    def test_launch_app_on_display_full_sequence(self):
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(stdout="", returncode=0),                        # open -a 抖音
                MagicMock(stdout="42\t抖音\tdouyin\n", returncode=0),      # poll
                MagicMock(stdout="", returncode=0),                        # move
            ]
            out = interp.execute(
                skill,
                {
                    "action_id": "launch_app_on_display",
                    "app": "抖音",
                    "display": "副",  # → BenQ
                },
            )
        assert out == "已打开 抖音 到 BenQ"
        calls = mock_run.call_args_list
        assert calls[0].args[0] == ["open", "-a", "抖音"]
        assert calls[1].args[0][:3] == ["aerospace", "list-windows", "--all"]
        assert calls[2].args[0] == [
            "aerospace", "move-node-to-monitor",
            "--window-id", "42", "BenQ",
        ]

    def test_move_app_to_display_finds_window_by_name_not_focus(self):
        """The pet-mode safety contract: this action must not depend on what
        is focused. Verifies it polls aerospace for the named app and moves
        that window directly, regardless of which window has focus."""
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.side_effect = [
                # poll_window: aerospace lists windows, 抖音 has wid 555,
                # plus a Jarvis window which must be ignored
                MagicMock(
                    stdout=(
                        "111\tJarvis\tpet-overlay\n"
                        "555\t抖音\tdouyin\n"
                    ),
                    returncode=0,
                ),
                # move
                MagicMock(stdout="", returncode=0),
            ]
            out = interp.execute(
                skill,
                {
                    "action_id": "move_app_to_display",
                    "app": "抖音",
                    "display": "副",
                },
            )
        assert out == "已把 抖音 挪到 BenQ"
        move_argv = mock_run.call_args_list[1].args[0]
        # Critical: --window-id is 555 (抖音), NOT 111 (Jarvis pet UI)
        assert move_argv == [
            "aerospace", "move-node-to-monitor",
            "--window-id", "555", "BenQ",
        ]

    def test_move_app_to_display_app_not_running_returns_error_template(self):
        interp, skill = self._load()
        # speed up the poll for this test
        skill["action"]["registry"]["move_app_to_display"]["steps"][0][
            "timeout_s"
        ] = 0.05
        skill["action"]["registry"]["move_app_to_display"]["steps"][0][
            "interval_s"
        ] = 0.01
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            out = interp.execute(
                skill,
                {
                    "action_id": "move_app_to_display",
                    "app": "Nonexistent",
                    "display": "BenQ",
                },
            )
        assert "Mac GUI" in out

    def test_move_focused_to_display_uses_alias(self):
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            out = interp.execute(
                skill, {"action_id": "move_focused_to_display", "display": "主"}
            )
        assert out == "已移到 Built-in"
        assert mock_run.call_args_list[0].args[0] == [
            "aerospace", "move-node-to-monitor", "Built-in",
        ]

    def test_focus_app_uses_argv_binding(self):
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            out = interp.execute(skill, {"action_id": "focus_app", "app": "Cursor"})
        assert out == "已切到 Cursor"
        argv = mock_run.call_args_list[0].args[0]
        assert argv[0] == "osascript"
        assert argv[1] == "-e"
        assert "on run argv" in argv[2]
        assert "tell application (item 1 of argv)" in argv[2]
        assert argv[-1] == "Cursor"

    def test_focus_app_blocks_applescript_injection(self):
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            evil = '"; do shell script "rm -rf /"; tell app "X'
            interp.execute(skill, {"action_id": "focus_app", "app": evil})
        argv = mock_run.call_args_list[0].args[0]
        # Evil flows to argv[3] only; script body never sees it.
        assert argv[-1] == evil
        assert "do shell script" not in argv[2]

    # ----- Launch & open ----------------------------------------------------

    def test_launch_app(self):
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            out = interp.execute(skill, {"action_id": "launch_app", "app": "Cursor"})
        assert out == "已打开 Cursor"
        assert mock_run.call_args_list[0].args[0] == ["open", "-a", "Cursor"]

    def test_open_url(self):
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            out = interp.execute(
                skill,
                {"action_id": "open_url", "url": "https://youtube.com"},
            )
        assert out == "已打开 https://youtube.com"
        assert mock_run.call_args_list[0].args[0] == ["open", "https://youtube.com"]

    def test_open_url_on_display_full_sequence(self):
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(stdout="", returncode=0),  # open chrome --app=
                MagicMock(  # poll: chrome window matches title hint
                    stdout="77\tGoogle Chrome\t抖音-记录美好生活\n",
                    returncode=0,
                ),
                MagicMock(stdout="", returncode=0),  # move
            ]
            out = interp.execute(
                skill,
                {
                    "action_id": "open_url_on_display",
                    "url": "https://www.douyin.com",
                    "title_hint": "抖音",
                    "display": "副",
                },
            )
        assert out == "已在 BenQ 打开 https://www.douyin.com"
        calls = mock_run.call_args_list
        assert calls[0].args[0] == [
            "open", "-na", "Google Chrome", "--args",
            "--app=https://www.douyin.com", "--new-window",
        ]
        assert calls[2].args[0] == [
            "aerospace", "move-node-to-monitor",
            "--window-id", "77", "BenQ",
        ]

    # ----- Window placement -------------------------------------------------

    def test_move_to_workspace(self):
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            out = interp.execute(
                skill,
                {"action_id": "move_to_workspace", "workspace": "3"},
            )
        assert out == "已扔到工作区 3"
        assert mock_run.call_args_list[0].args[0] == [
            "aerospace", "move-node-to-workspace", "3",
        ]

    # ----- Focus ------------------------------------------------------------

    def test_focus_monitor(self):
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            out = interp.execute(
                skill,
                {"action_id": "focus_monitor", "display": "副"},
            )
        assert out == "聚焦到 BenQ"
        assert mock_run.call_args_list[0].args[0] == [
            "aerospace", "focus-monitor", "BenQ",
        ]

    def test_focus_back_and_forth(self):
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            out = interp.execute(skill, {"action_id": "focus_back_and_forth"})
        assert out == "切回上一个窗口"
        assert mock_run.call_args_list[0].args[0] == [
            "aerospace", "focus-back-and-forth",
        ]

    # ----- Workspace --------------------------------------------------------

    def test_workspace_switch(self):
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            out = interp.execute(
                skill,
                {"action_id": "workspace_switch", "workspace": "2"},
            )
        assert out == "已切到工作区 2"
        assert mock_run.call_args_list[0].args[0] == ["aerospace", "workspace", "2"]

    # ----- Window state -----------------------------------------------------

    def test_fullscreen_focused(self):
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            out = interp.execute(skill, {"action_id": "fullscreen_focused"})
        assert out == "全屏已切换"
        assert mock_run.call_args_list[0].args[0] == [
            "aerospace", "macos-native-fullscreen",
        ]

    def test_minimize_focused(self):
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            out = interp.execute(skill, {"action_id": "minimize_focused"})
        assert out == "已最小化"
        assert mock_run.call_args_list[0].args[0] == [
            "aerospace", "macos-native-minimize",
        ]

    def test_close_focused(self):
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            out = interp.execute(skill, {"action_id": "close_focused"})
        assert out == "已关闭"
        assert mock_run.call_args_list[0].args[0] == ["aerospace", "close"]

    def test_close_all_but_current(self):
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            out = interp.execute(skill, {"action_id": "close_all_but_current"})
        assert out == "已关闭其他窗口"
        assert mock_run.call_args_list[0].args[0] == [
            "aerospace", "close-all-windows-but-current",
        ]

    # ----- System -----------------------------------------------------------

    def test_set_volume(self):
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            out = interp.execute(
                skill, {"action_id": "set_volume", "level": "30"}
            )
        assert out == "音量调到 30"
        argv = mock_run.call_args_list[0].args[0]
        assert argv[0] == "osascript"
        assert "set volume output volume" in argv[2]
        assert argv[-1] == "30"

    def test_set_volume_user_input_only_via_argv(self):
        # Injection guard: level value flows via argv, not script body.
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            evil = "100); do shell script \"rm -rf /\"; ("
            interp.execute(skill, {"action_id": "set_volume", "level": evil})
        argv = mock_run.call_args_list[0].args[0]
        assert "do shell script" not in argv[2]
        assert argv[-1] == evil

    def test_mute_toggle(self):
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            out = interp.execute(skill, {"action_id": "mute_toggle"})
        assert out == "静音已切换"
        argv = mock_run.call_args_list[0].args[0]
        assert argv[0] == "osascript"
        assert "output muted" in argv[2]
        assert "set volume with output muted" in argv[2]
        assert "set volume without output muted" in argv[2]
        # mute_toggle reads+flips internally, no argv binding
        assert len(argv) == 3  # ["osascript", "-e", <script>]

    def test_lock_screen(self):
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            out = interp.execute(skill, {"action_id": "lock_screen"})
        assert out == "锁屏中"
        argv = mock_run.call_args_list[0].args[0]
        assert argv[0] == "osascript"
        # Ctrl+Cmd+Q is the macOS lock shortcut
        assert "control down" in argv[2]
        assert "command down" in argv[2]
        assert 'keystroke "q"' in argv[2]

    def test_screenshot(self):
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            out = interp.execute(skill, {"action_id": "screenshot"})
        assert out == "已截屏到剪贴板"
        # -c clipboard, -x silent
        assert mock_run.call_args_list[0].args[0] == ["screencapture", "-c", "-x"]

    def test_aerospace_missing_returns_error_template(self):
        interp, skill = self._load()
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(stdout="", returncode=0),  # open succeeds
                FileNotFoundError("aerospace"),      # poll: aerospace missing
            ]
            out = interp.execute(
                skill,
                {
                    "action_id": "launch_app_on_display",
                    "app": "抖音",
                    "display": "BenQ",
                },
            )
        assert "Mac GUI" in out

    def test_window_never_appears_returns_error_template(self):
        interp, skill = self._load()
        # speed up: shrink poll timeout for this test
        skill["action"]["registry"]["launch_app_on_display"]["steps"][1][
            "timeout_s"
        ] = 0.05
        skill["action"]["registry"]["launch_app_on_display"]["steps"][1][
            "interval_s"
        ] = 0.01
        with patch("core.yaml_interpreter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            out = interp.execute(
                skill,
                {
                    "action_id": "launch_app_on_display",
                    "app": "Nonexistent",
                    "display": "BenQ",
                },
            )
        assert "Mac GUI" in out

    def test_marked_not_read_only_destructive(self):
        _, skill = self._load()
        # mac_gui mutates window state; should not be marked read_only.
        assert skill["annotations"]["read_only"] is False

    def test_registered_via_tool_registry(self, tmp_path):
        # End-to-end: ToolRegistry should pick up skills/mac_gui.yaml and
        # surface a tool named "mac_gui".
        from core.tool_registry import ToolRegistry
        registry = ToolRegistry({}, yaml_dirs=[
            os.path.join(os.path.dirname(__file__), "..", "skills"),
        ])
        defs = registry.get_tool_definitions(user_role="owner")
        names = {d["name"] for d in defs}
        assert "mac_gui" in names
