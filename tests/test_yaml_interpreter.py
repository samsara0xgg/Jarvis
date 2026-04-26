"""Tests for the YAMLInterpreter."""

import json
import os
import tempfile
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
