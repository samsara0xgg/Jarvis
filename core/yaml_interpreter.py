"""YAML skill interpreter — load, validate, and execute YAML-defined skills.

Two action types:
    http_get / http_post — outbound HTTP with retry, domain whitelist,
                           and private-IP block.
    file_write           — local file write with mandatory ``allowed_root``
                           guard against path traversal / symlink escape.

Templates render in a Jinja2 sandbox extended with two helpers:
    slug    — filter: NFKD ASCII kebab; Chinese-literal fallback.
    now()   — global: timestamp in jarvis schema (YYYY-MM-DD-HHMM).

Three-layer error handling: action-specific failure → ``error_template`` →
hard-coded fallback string.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
import yaml
from jinja2.sandbox import ImmutableSandboxedEnvironment

LOGGER = logging.getLogger(__name__)

_PRIVATE_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
]

_FALLBACK_ERROR = "技能执行失败，请稍后再试"

_SLUG_MAX_WORDS = 5
_SLUG_MAX_CHARS = 60


def _is_private_url(url: str) -> bool:
    """Check if *url* points to a private/local network address."""
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    if hostname in ("localhost", ""):
        return True
    try:
        addr = ipaddress.ip_address(hostname)
        return any(addr in net for net in _PRIVATE_NETWORKS)
    except ValueError:
        return False  # hostname is a domain name, not an IP


def _slug_filter(text: Any) -> str:
    """Jinja2 filter ``slug``: convert text to filename slug.

    ASCII letters extracted from ``text`` join as a 1-5 word kebab.
    If no ASCII letters are present (typical Chinese voice notes), the
    cleaned literal text is used instead — Unicode filenames are
    filesystem-safe on macOS / iOS / linux. Empty input yields ``note``.
    """
    if text is None:
        return "note"
    raw = str(text)
    norm = unicodedata.normalize("NFKD", raw)
    ascii_only = norm.encode("ascii", "ignore").decode("ascii")
    words = re.findall(r"[a-zA-Z]+", ascii_only.lower())
    if words:
        slug = "-".join(words[:_SLUG_MAX_WORDS])
        return slug[:_SLUG_MAX_CHARS]
    cleaned = re.sub(r"\s+", "", raw)
    cleaned = re.sub(r"[\\/:*?\"<>|]", "", cleaned)[:_SLUG_MAX_CHARS]
    return cleaned or "note"


def _now_global(fmt: str = "%Y-%m-%d-%H%M") -> str:
    """Jinja2 global ``now``: current timestamp in *fmt*."""
    return datetime.now().strftime(fmt)


class YAMLInterpreter:
    """Execute YAML skill definitions.

    Lifecycle per call::

        load_skill(path)         →  skill dict
        to_tool_definition(skill) →  OpenAI-compatible schema
        execute(skill, params)    →  rendered string
    """

    def __init__(self) -> None:
        self._env = ImmutableSandboxedEnvironment()
        self._env.filters["slug"] = _slug_filter
        self._env.globals["now"] = _now_global

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_skill(self, yaml_path: str) -> dict:
        """Load a YAML skill file and return its dict."""
        with open(yaml_path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)

    def to_tool_definition(self, skill: dict) -> dict:
        """Convert a YAML skill dict to an OpenAI-compatible tool definition.

        Returns::

            {
                "name": ...,
                "description": ...,
                "input_schema": {"type": "object", "properties": {...}, "required": [...]}
            }
        """
        properties: dict[str, Any] = {}
        required: list[str] = []
        for param in skill.get("parameters", []):
            properties[param["name"]] = {
                "type": param.get("type", "string"),
                "description": param.get("description", ""),
            }
            if param.get("required", False):
                required.append(param["name"])

        return {
            "name": skill["name"],
            "description": skill.get("description", ""),
            "input_schema": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        }

    def execute(self, skill: dict, params: dict) -> str:
        """Execute a YAML skill and return the rendered response string.

        Dispatches by ``action.type``:
            http_get / http_post → HTTP path with retry + security checks
            file_write           → local file write under allowed_root
        """
        params = self._apply_defaults(skill, dict(params))
        action = skill.get("action", {})
        atype = action.get("type", "http_get")

        if atype in ("http_get", "http_post"):
            return self._execute_http(skill, params)
        if atype == "file_write":
            return self._execute_file_write(skill, params)

        msg = f"Unsupported action type: {atype}"
        LOGGER.warning(msg)
        return msg

    # ------------------------------------------------------------------
    # Action executors
    # ------------------------------------------------------------------

    def _execute_http(self, skill: dict, params: dict) -> str:
        """HTTP path: render URL, security checks, call, render response."""
        action = skill["action"]
        response_cfg = skill.get("response", {})
        error_template = response_cfg.get("error_template")

        url = self._render(action["url"], params)

        allowed = skill.get("security", {}).get("allowed_domains", [])
        if allowed:
            parsed = urlparse(url)
            if parsed.hostname not in allowed:
                msg = f"Domain not allowed: {parsed.hostname}"
                LOGGER.warning(msg)
                return msg

        if _is_private_url(url):
            msg = f"Blocked: private/local address ({urlparse(url).hostname})"
            LOGGER.warning(msg)
            return msg

        try:
            resp = self._http_call(action, url, params, skill)
        except Exception:
            LOGGER.exception("All retries exhausted for %s", skill.get("name"))
            return error_template if error_template else _FALLBACK_ERROR

        try:
            return self._render_response(response_cfg, params, resp.json())
        except Exception:
            LOGGER.exception("Response rendering failed for %s", skill.get("name"))
            return error_template if error_template else _FALLBACK_ERROR

    def _execute_file_write(self, skill: dict, params: dict) -> str:
        """File write path: render path/content under allowed_root, write."""
        action = skill["action"]
        response_cfg = skill.get("response", {})
        error_template = response_cfg.get("error_template")

        try:
            path = self._resolve_safe_path(action, params)
        except PermissionError as exc:
            LOGGER.warning("Path-traversal blocked for %s: %s", skill.get("name"), exc)
            return str(exc)
        except Exception:
            LOGGER.exception("Path render failed for %s", skill.get("name"))
            return error_template if error_template else _FALLBACK_ERROR

        try:
            content = self._render(action.get("content", ""), params)
        except Exception:
            LOGGER.exception("Content render failed for %s", skill.get("name"))
            return error_template if error_template else _FALLBACK_ERROR

        try:
            if action.get("create_parents", True):
                path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError:
            LOGGER.exception("File write failed for %s", skill.get("name"))
            return error_template if error_template else _FALLBACK_ERROR

        template = response_cfg.get("template", "Saved.")
        ctx = {**params, "file_path": str(path), "filename": path.name}
        try:
            return self._render(template, ctx)
        except Exception:
            LOGGER.exception("Response render failed for %s", skill.get("name"))
            return error_template if error_template else _FALLBACK_ERROR

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_safe_path(self, action: dict, params: dict) -> Path:
        """Render path template and verify it stays inside ``allowed_root``.

        Raises ``PermissionError`` if the resolved path escapes
        ``allowed_root`` (covers ``..`` traversal, absolute injection,
        and symlink escape — ``Path.resolve()`` follows symlinks).
        """
        if "path" not in action:
            raise PermissionError("file_write requires action.path")
        rendered = self._render(action["path"], params)
        path = Path(rendered).expanduser().resolve()

        allowed_root = action.get("allowed_root")
        if not allowed_root:
            raise PermissionError("file_write requires action.allowed_root")
        allowed = Path(str(allowed_root)).expanduser().resolve()

        if path != allowed and not path.is_relative_to(allowed):
            raise PermissionError(
                f"Path escape blocked: {path} not under {allowed}"
            )
        return path

    def _apply_defaults(self, skill: dict, params: dict) -> dict:
        """Fill missing params with their declared defaults."""
        for param_def in skill.get("parameters", []):
            name = param_def["name"]
            if name not in params and "default" in param_def:
                params[name] = param_def["default"]
        return params

    def _render(self, template_str: str, context: dict) -> str:
        """Render a Jinja2 template string inside the sandbox."""
        tpl = self._env.from_string(template_str)
        return tpl.render(**context)

    def _http_call(self, action: dict, url: str, params: dict, skill: dict) -> requests.Response:
        """Make an HTTP call with retry + exponential backoff.

        Raises on exhaustion so the caller can fall through to error layers.
        """
        retry_cfg = action.get("retry", {})
        max_attempts = retry_cfg.get("max", 1)
        delay_s = retry_cfg.get("delay_ms", 1000) / 1000.0
        backoff = retry_cfg.get("backoff", "exponential")
        timeout_s = action.get("timeout_ms", 10000) / 1000.0

        headers = dict(action.get("headers", {}))

        # Inject auth header from env if configured
        auth_env = skill.get("security", {}).get("auth_env")
        if auth_env:
            token = os.environ.get(auth_env, "")
            if token:
                headers["Authorization"] = f"Bearer {token}"

        last_exc: Exception | None = None
        for attempt in range(max_attempts):
            try:
                if action["type"] == "http_post":
                    body = self._render_body(action.get("body", {}), params)
                    resp = requests.post(url, headers=headers, json=body, timeout=timeout_s)
                else:
                    resp = requests.get(url, headers=headers, timeout=timeout_s)

                if resp.ok:
                    return resp
                resp.raise_for_status()
            except Exception as exc:
                last_exc = exc
                LOGGER.warning(
                    "HTTP attempt %d/%d failed for %s: %s",
                    attempt + 1,
                    max_attempts,
                    url,
                    exc,
                )
                if attempt < max_attempts - 1:
                    sleep_time = min(delay_s * (2 ** attempt if backoff == "exponential" else 1), 5.0)
                    time.sleep(sleep_time)

        raise last_exc  # type: ignore[misc]

    def _render_body(self, body_template: dict, context: dict) -> dict:
        """Render each value in an HTTP POST body dict."""
        rendered: dict[str, Any] = {}
        for key, val in body_template.items():
            if isinstance(val, str):
                rendered[key] = self._render(val, context)
            else:
                rendered[key] = val
        return rendered

    def _render_response(self, response_cfg: dict, params: dict, result: Any) -> str:
        """Three-step response rendering: extract → compute → template."""
        context: dict[str, Any] = {**params, "result": result}

        # Step 1: extract
        for key, tpl in response_cfg.get("extract", {}).items():
            context[key] = self._render(tpl, context)

        # Step 2: compute — try to convert extracted strings to floats
        for key, tpl in response_cfg.get("compute", {}).items():
            num_ctx = dict(context)
            for k, v in num_ctx.items():
                if isinstance(v, str):
                    try:
                        num_ctx[k] = float(v)
                    except (ValueError, TypeError):
                        pass
            raw = self._render(tpl, num_ctx)
            try:
                context[key] = float(raw)
            except (ValueError, TypeError):
                context[key] = raw

        # Step 3: template
        template_str = response_cfg.get("template", "")
        return self._render(template_str, context)
