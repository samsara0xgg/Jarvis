"""YAML skill interpreter — load, validate, and execute YAML-defined skills.

Renders URLs and response templates with Jinja2 (sandboxed), enforces domain
whitelists and private-IP blocking, and retries HTTP calls with exponential
backoff.  Three-layer error handling: retry → error_template → fallback string.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import time
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


class YAMLInterpreter:
    """Execute YAML skill definitions.

    Lifecycle per call::

        load_skill(path)  →  skill dict
        to_tool_definition(skill)  →  OpenAI-compatible schema
        execute(skill, params)  →  rendered string
    """

    def __init__(self) -> None:
        self._env = ImmutableSandboxedEnvironment()

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

        Steps:
            1. Apply parameter defaults
            2. Render action URL
            3. Security checks (domain whitelist, private IP)
            4. HTTP call with retry
            5. Render response (extract → compute → template)
            6. On failure: error_template (layer 2) → fallback (layer 3)
        """
        # 1. Apply defaults
        params = self._apply_defaults(skill, dict(params))

        # 2. Render URL
        action = skill["action"]
        url = self._render(action["url"], params)

        # 3. Security: domain whitelist
        allowed = skill.get("security", {}).get("allowed_domains", [])
        if allowed:
            parsed = urlparse(url)
            if parsed.hostname not in allowed:
                msg = f"Domain not allowed: {parsed.hostname}"
                LOGGER.warning(msg)
                return msg

        # 3b. Security: private IP block
        if _is_private_url(url):
            msg = f"Blocked: private/local address ({urlparse(url).hostname})"
            LOGGER.warning(msg)
            return msg

        # 4. HTTP call with retry
        response_cfg = skill.get("response", {})
        error_template = response_cfg.get("error_template")

        try:
            resp = self._http_call(action, url, params, skill)
        except Exception:
            LOGGER.exception("All retries exhausted for %s", skill.get("name"))
            return error_template if error_template else _FALLBACK_ERROR

        # 5. Render response
        try:
            return self._render_response(response_cfg, params, resp.json())
        except Exception:
            LOGGER.exception("Response rendering failed for %s", skill.get("name"))
            return error_template if error_template else _FALLBACK_ERROR

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

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
            # Build a numeric-aware context for compute expressions
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
