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
