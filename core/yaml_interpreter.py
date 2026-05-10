"""YAML skill interpreter — load, validate, and execute YAML-defined skills.

Action types:
    http_get / http_post — outbound HTTP with retry, domain whitelist,
                           and private-IP block.
    file_write           — local file write with mandatory ``allowed_root``
                           guard against path traversal / symlink escape.
    macos_paste          — pbcopy + AppleScript Cmd+V into focused or
                           named app (case-insensitive resolver).
    zellij_send          — inject text (and optional Enter) into a named
                           zellij session via ``zellij action write-chars``.

Templates render in a Jinja2 sandbox extended with two helpers:
    slug    — filter: NFKD ASCII kebab; Chinese-literal fallback.
    now()   — global: timestamp in jarvis schema (YYYY-MM-DD-HHMM).

Three-layer error handling: action-specific failure → ``error_template`` →
hard-coded fallback string.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import subprocess
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
import yaml
from jinja2.sandbox import ImmutableSandboxedEnvironment

from core import cc_jsonl_reader
from core.tool_result import SUCCESS, make_tool_result

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

# AppleScript for paste: hardcoded constant — no user input ever flows here.
# User text reaches the focused app only via the system clipboard (pbcopy).
_APPLESCRIPT_PASTE = (
    'tell application "System Events" to keystroke "v" using command down'
)
_APPLESCRIPT_CMDTAB = (
    'tell application "System Events" to keystroke tab using command down'
)
_APPLESCRIPT_DELAY = "delay 0.2"
_PREV_KEYWORDS = {"prev", "previous", "上一个", "前一个"}
_PBCOPY_TIMEOUT_S = 2.0
_OSASCRIPT_TIMEOUT_S = 5.0

_ZELLIJ_TIMEOUT_S = 3.0
_ZELLIJ_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ZELLIJ_ENTER_BYTE = "13"  # CR — what a real keyboard sends on Enter

# cc_read_state: cap on the dump-screen text returned to the LLM. Average
# zellij viewport is ~3KB; 8KB headroom covers wider terminals while
# keeping tool-result tokens bounded.
_CC_READ_DUMP_MAX_BYTES = 8000
# Number of trailing viewport lines forwarded as "ui_tail" — captures
# current UI state (permission prompts, "Done" markers, in-flight
# rendering) for LLM matching against jsonl candidates.
_CC_READ_UI_TAIL_LINES = 25
# Number of recent jsonl candidates to feed the chat LLM. The LLM
# correlates each candidate's user/assistant/tool fingerprint against
# the viewport to pick the right one — no deterministic pane→jsonl
# binding is required. 3 covers Allen's typical concurrent cc count.
_CC_READ_CANDIDATE_COUNT = 3

# aerospace_op step timeouts. List/poll values tuned for AeroSpace 0.20.x
# on M2 Max — list-windows returns in ~30ms warm, ~150ms cold; window-spawn
# after ``open -a`` is typically 0.3-2s, can stretch to 4s for Electron
# cold-starts. Step default 5s covers everything except the explicit
# poll_window step which gets its own 8s budget.
_AEROSPACE_STEP_TIMEOUT_S = 5.0
_AEROSPACE_POLL_TIMEOUT_S = 8.0
_AEROSPACE_POLL_INTERVAL_S = 0.3
_AEROSPACE_LIST_TIMEOUT_S = 2.0
_AEROSPACE_LIST_FORMAT = "%{window-id}\t%{app-name}\t%{window-title}"


class _PaneNotFound(LookupError):
    """Raised when ``_resolve_zellij_pane`` finds no pane with the given title."""


class _PollTimeout(TimeoutError):
    """Raised when ``_step_poll_window`` does not find a matching window in time."""


def _format_relative_time(seconds: float) -> str:
    """Compact 'X 前' style relative-time string for cc candidate timestamps."""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{int(seconds)} 秒"
    if seconds < 3600:
        return f"{int(seconds / 60)} 分钟"
    if seconds < 86400:
        return f"{int(seconds / 3600)} 小时"
    return f"{int(seconds / 86400)} 天"


def _render_cc_candidate(
    idx: int,
    cand: "cc_jsonl_reader.CCCandidate",
    now_epoch: float,
) -> str:
    """Format one cc jsonl candidate as a fingerprint block for LLM matching.

    The structure (user prompt → assistant text → tool calls) gives the
    chat LLM three independent fingerprints to correlate against the
    pane viewport. user prompts are usually the most distinctive — short,
    human-written, often verbatim in the viewport.
    """
    rel = _format_relative_time(now_epoch - cand.mtime)
    sid_short = cand.session_id[:8] if cand.session_id else "?"
    lines = [f"### 候选 {idx} — session {sid_short} — {rel}前"]
    if cand.last_user_prompt:
        lines.append(f"↳ user 最近问：{cand.last_user_prompt}")
    if cand.last_assistant_text:
        lines.append(f"↳ assistant 最近答：{cand.last_assistant_text}")
    if cand.recent_tool_calls:
        tools_str = " / ".join(
            f"{tc.name}({tc.input_summary})" if tc.input_summary else tc.name
            for tc in cand.recent_tool_calls
        )
        lines.append(f"↳ tool calls：{tools_str}")
    return "\n".join(lines)

# Whitelist of special keys for the zellij_send ``keys`` parameter.
# Each value is a space-separated decimal byte sequence accepted by
# ``zellij action write <bytes>``. Whitelist (not arbitrary bytes) is
# deliberate: voice → LLM may hallucinate exotic sequences, so we
# constrain to the small set actually needed for cc control.
_ZELLIJ_KEYS_MAP: dict[str, str] = {
    "c-c": "3",          # Ctrl+C — interrupt
    "c-d": "4",          # Ctrl+D — EOF / exit
    "c-z": "26",         # Ctrl+Z — suspend (rarely useful for cc but cheap)
    "esc": "27",         # Escape — dismiss autocomplete / cancel
    "tab": "9",          # Tab — accept suggestion
    "up": "27 91 65",    # ESC [ A — history up
    "down": "27 91 66",  # ESC [ B — history down
    "left": "27 91 68",  # ESC [ D
    "right": "27 91 67", # ESC [ C
}

_APP_DIRS = ("/Applications", "/Applications/Utilities", "/System/Applications")


def _resolve_app_name(target: str) -> str:
    """Case-insensitive match against installed .app bundles.

    AppleScript's ``tell application "X"`` is case-sensitive — "iterm"
    fails where "iTerm" works. To make voice input forgiving, we map
    user-supplied target to the canonical .app name found in standard
    locations. Falls back to the original string if no match (osascript
    will then fail clean and the error_template fires).
    """
    target_lower = target.strip().lower()
    if not target_lower:
        return target
    for app_dir in _APP_DIRS:
        try:
            entries = os.listdir(app_dir)
        except OSError:
            continue
        for entry in entries:
            if entry.endswith(".app") and entry[:-4].lower() == target_lower:
                return entry[:-4]
    return target


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
            macos_paste          → clipboard + AppleScript focus + paste
            zellij_send          → zellij CLI write-chars into a session
        """
        params = self._apply_defaults(skill, dict(params))
        action = skill.get("action", {})
        atype = action.get("type", "http_get")

        if atype in ("http_get", "http_post"):
            return self._execute_http(skill, params)
        if atype == "file_write":
            return self._execute_file_write(skill, params)
        if atype == "macos_paste":
            return self._execute_macos_paste(skill, params)
        if atype == "zellij_send":
            return self._execute_zellij_send(skill, params)
        if atype == "cc_read_state":
            return self._execute_cc_read_state(skill, params)
        if atype == "aerospace_op":
            return self._execute_aerospace_op(skill, params)

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

    def _execute_macos_paste(self, skill: dict, params: dict) -> str:
        """macos_paste path: render text + optional target → pbcopy → focus → Cmd+V.

        Optional ``target`` field selects where the paste lands:
            empty / unset    paste to current frontmost (no focus change)
            "prev"/"上一个"   send Cmd+Tab to flip back to the previous app
            <app name>       activate that macOS app via argv binding

        Security model: the AppleScript fragments are module-level constants
        (no template interpolation). User text reaches the focused app only
        via the system clipboard. App-name targets pass through osascript
        argv binding (``on run argv ... item 1 of argv``), so AppleScript
        sees the name as a literal string — shell or AppleScript metachars
        in target are inert. Subprocess uses argv lists (no shell), so
        nothing in text or target ever reaches a shell.

        Errors:
            - empty text → "无内容可输入"
            - subprocess timeout / non-zero exit → error_template
              (covers first-run macOS Accessibility permission denial)
            - osascript missing (non-macOS) → "Not on macOS"
        """
        action = skill["action"]
        response_cfg = skill.get("response", {})
        error_template = response_cfg.get("error_template")

        try:
            text = self._render(action.get("text", ""), params)
            target = ""
            if "target" in action:
                target = self._render(action["target"], params).strip()
        except Exception:
            LOGGER.exception("macos_paste render failed for %s", skill.get("name"))
            return error_template if error_template else _FALLBACK_ERROR

        if not text:
            return "无内容可输入"

        osascript_argv = self._build_paste_argv(target)

        try:
            subprocess.run(
                ["pbcopy"],
                input=text,
                text=True,
                check=True,
                timeout=_PBCOPY_TIMEOUT_S,
            )
            subprocess.run(
                osascript_argv,
                capture_output=True,
                check=True,
                timeout=_OSASCRIPT_TIMEOUT_S,
            )
        except FileNotFoundError:
            LOGGER.warning("macos_paste: pbcopy or osascript not found (non-macOS?)")
            return error_template if error_template else "Not on macOS"
        except subprocess.TimeoutExpired:
            LOGGER.warning("macos_paste: subprocess timeout")
            return error_template if error_template else "输入超时"
        except subprocess.CalledProcessError as exc:
            LOGGER.warning("macos_paste: subprocess failed: %s", exc)
            return error_template if error_template else _FALLBACK_ERROR

        template = response_cfg.get("template", "已输入")
        try:
            return self._render(template, {**params, "text": text, "target": target})
        except Exception:
            LOGGER.exception("macos_paste response render failed for %s", skill.get("name"))
            return error_template if error_template else _FALLBACK_ERROR

    def _execute_zellij_send(self, skill: dict, params: dict) -> str:
        """zellij_send: write text and/or special keys into a zellij session.

        Three optional inputs combined per call (in order of execution):
            keys   : list[str] of whitelisted key names (see ``_ZELLIJ_KEYS_MAP``).
                     Each maps to a byte sequence sent via
                     ``zellij action write <bytes>``. Sent first.
            text   : string sent via ``zellij action write-chars``. Sent second.
            submit : bool — appends Enter (CR=13). Sent last.

        At least one of ``keys`` or ``text`` must be provided.

        Security model:
            - subprocess uses argv lists (shell=False), so user text in
              argv[N] is shell-inert: metachars cannot trigger expansion
            - session name validated against ``[A-Za-z0-9_-]{1,64}`` BEFORE
              reaching subprocess, blocking flag injection (e.g. a session
              like ``--config /etc/passwd`` would otherwise be parsed as
              another zellij flag)
            - keys constrained to a fixed whitelist — voice → LLM might
              hallucinate exotic byte sequences; whitelist forces every
              new control key to be added in code review
            - text content is opaque to zellij — it just relays bytes to
              the target pane's PTY stdin. The receiving program (cc) is
              responsible for input sanitization on its side.

        Errors:
            - empty text AND empty keys → "无内容可发送"
            - invalid session name → returned as error message
            - unknown key name → returned as error message
            - zellij not in PATH → "zellij 未安装"
            - subprocess CalledProcessError → error_template (most common
              cause: session does not exist; zellij prints "no such
              session" to stderr)
            - subprocess timeout → "发送超时"
            - any subprocess in the keys → text → submit chain fails
              fast; later steps are skipped on first error
        """
        action = skill["action"]
        response_cfg = skill.get("response", {})
        error_template = response_cfg.get("error_template")

        try:
            text = self._render(action.get("text", ""), params)
            session = self._render(action.get("session", "cc"), params).strip()
            submit = bool(action.get("submit", False))
            keys_raw = action.get("keys", []) or []
            keys = [str(k).strip().lower() for k in keys_raw]
        except Exception:
            LOGGER.exception("zellij_send render failed for %s", skill.get("name"))
            return error_template if error_template else _FALLBACK_ERROR

        if not text and not keys:
            return "无内容可发送"

        if not _ZELLIJ_SESSION_RE.match(session):
            msg = f"非法 zellij session 名: {session!r}"
            LOGGER.warning(msg)
            return error_template if error_template else msg

        for k in keys:
            if k not in _ZELLIJ_KEYS_MAP:
                msg = f"未知 key: {k!r}（白名单: {sorted(_ZELLIJ_KEYS_MAP)}）"
                LOGGER.warning(msg)
                return error_template if error_template else msg

        base_argv = ["zellij", "--session", session, "action"]

        try:
            for k in keys:
                byte_seq = _ZELLIJ_KEYS_MAP[k].split()
                subprocess.run(
                    [*base_argv, "write", *byte_seq],
                    capture_output=True,
                    check=True,
                    timeout=_ZELLIJ_TIMEOUT_S,
                )
            if text:
                subprocess.run(
                    [*base_argv, "write-chars", text],
                    capture_output=True,
                    check=True,
                    timeout=_ZELLIJ_TIMEOUT_S,
                )
            if submit:
                subprocess.run(
                    [*base_argv, "write", _ZELLIJ_ENTER_BYTE],
                    capture_output=True,
                    check=True,
                    timeout=_ZELLIJ_TIMEOUT_S,
                )
        except FileNotFoundError:
            LOGGER.warning("zellij_send: zellij not in PATH")
            return error_template if error_template else "zellij 未安装"
        except subprocess.TimeoutExpired:
            LOGGER.warning("zellij_send: subprocess timeout")
            return error_template if error_template else "发送超时"
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or b"").decode("utf-8", "replace").strip()
            LOGGER.warning(
                "zellij_send: zellij failed (exit=%s): %s", exc.returncode, stderr
            )
            return error_template if error_template else f"zellij 错误: {stderr[:80]}"

        template = response_cfg.get("template", "已发送")
        try:
            message = self._render(
                template, {**params, "text": text, "session": session, "keys": keys}
            )
            if response_cfg.get("envelope"):
                return make_tool_result(
                    str(response_cfg.get("status", SUCCESS)),
                    message,
                )
            return message
        except Exception:
            LOGGER.exception("zellij_send response render failed for %s", skill.get("name"))
            return error_template if error_template else _FALLBACK_ERROR

    def _execute_cc_read_state(self, skill: dict, params: dict) -> str:
        """cc_read_state: read a cc pane's recent state, plus jsonl candidates.

        Returns three sections to the chat LLM:

        1. The dump-screen viewport of the named pane — deterministic
           UI state (permission prompts, ✻ Done markers, in-flight
           rendering).
        2. Up to ``_CC_READ_CANDIDATE_COUNT`` recent jsonl candidates
           from the pane's cwd, each with last-user-prompt /
           last-assistant-text / recent tool calls. The LLM
           content-correlates the viewport against these candidates
           to pick the right one — no deterministic pane→jsonl
           binding is required.
        3. Matching instructions telling the LLM how to combine the
           viewport with the matched candidate.

        Action params:
            session    : zellij session name (default "cc"). Validated
                         by ``_ZELLIJ_SESSION_RE``.
            pane_title : zellij pane title to target (default "Pane #2"
                         — Allen's primary cc pane). Resolved at call
                         time via ``zellij action list-panes --json``;
                         pane_id is not stored so layout reordering
                         doesn't break the link.

        On dump-screen failure, the error template fires. Candidate
        lookup failure (no jsonl found) degrades gracefully — the LLM
        still gets the viewport.
        """
        action = skill["action"]
        response_cfg = skill.get("response", {})
        error_template = response_cfg.get("error_template")

        try:
            session = self._render(action.get("session", "cc"), params).strip()
            pane_title = self._render(
                action.get("pane_title", "Pane #2"), params
            ).strip()
        except Exception:
            LOGGER.exception("cc_read_state render failed for %s", skill.get("name"))
            return error_template if error_template else _FALLBACK_ERROR

        if not _ZELLIJ_SESSION_RE.match(session):
            msg = f"非法 zellij session 名: {session!r}"
            LOGGER.warning(msg)
            return error_template if error_template else msg

        try:
            pane_id, pane_cwd = self._resolve_zellij_pane(session, pane_title)
        except FileNotFoundError:
            return error_template if error_template else "zellij 未安装"
        except subprocess.TimeoutExpired:
            return error_template if error_template else "zellij 响应超时"
        except _PaneNotFound as exc:
            LOGGER.warning("cc_read_state: pane not found: %s", exc)
            return error_template if error_template else str(exc)
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or b"").decode("utf-8", "replace").strip()
            LOGGER.warning("cc_read_state: list-panes failed: %s", stderr)
            return error_template if error_template else f"zellij 错误: {stderr[:80]}"

        # Dump-screen — viewport text, no ANSI (LLM doesn't need colors).
        try:
            dump_result = subprocess.run(
                [
                    "zellij",
                    "--session",
                    session,
                    "action",
                    "dump-screen",
                    "--pane-id",
                    pane_id,
                ],
                capture_output=True,
                text=True,
                timeout=_ZELLIJ_TIMEOUT_S,
                check=True,
            )
            dump_text = dump_result.stdout
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip() or (exc.stdout or "").strip()
            LOGGER.warning("cc_read_state: dump-screen failed: %s", stderr)
            return error_template if error_template else f"zellij 错误: {stderr[:80]}"
        except subprocess.TimeoutExpired:
            return error_template if error_template else "dump-screen 超时"

        if len(dump_text) > _CC_READ_DUMP_MAX_BYTES:
            dump_text = "...[viewport truncated]...\n" + dump_text[-_CC_READ_DUMP_MAX_BYTES:]

        ui_tail = "\n".join(dump_text.rstrip().splitlines()[-_CC_READ_UI_TAIL_LINES:])

        # jsonl candidates — let LLM correlate against viewport. No
        # deterministic mapping; LLM picks based on content match.
        candidates: list[cc_jsonl_reader.CCCandidate] = []
        if pane_cwd:
            for jsonl_path in cc_jsonl_reader.find_recent_jsonls(
                pane_cwd, n=_CC_READ_CANDIDATE_COUNT
            ):
                cand = cc_jsonl_reader.read_recent_exchange(jsonl_path)
                if not cand.empty:
                    candidates.append(cand)

        formatted = self._format_cc_state_with_candidates(
            pane_title, ui_tail, candidates
        )
        template = response_cfg.get("template", "{{ result }}")
        try:
            return self._render(
                template,
                {
                    **params,
                    "result": formatted,
                    "session": session,
                    "pane_title": pane_title,
                    "pane_id": pane_id,
                },
            )
        except Exception:
            LOGGER.exception("cc_read_state response render failed for %s", skill.get("name"))
            return error_template if error_template else _FALLBACK_ERROR

    def _execute_aerospace_op(self, skill: dict, params: dict) -> str:
        """aerospace_op: dispatch to one of N declared operations and run its steps.

        ``skill["action"]`` carries:
            registry         : dict of action_id → {steps, response}
            display_aliases  : optional dict (e.g. ``副 → BenQ``) pre-applied to
                               ``params["display"]`` before any step renders

        ``params["action_id"]`` selects the entry from ``registry``. Each
        entry's ``steps`` is executed in order. Steps may capture output
        into ``params`` for downstream steps (e.g. ``poll_window`` writes a
        window-id to ``params["wid"]`` which a later ``shell`` step
        interpolates into ``aerospace move-node-to-monitor --window-id ...``).

        Step types:
            shell        — subprocess.run(cmd, check=True). All argv parts
                           rendered via Jinja2 against current params. May set
                           ``capture_var`` to bind stripped stdout into params.
            applescript  — osascript -e <script> [argv...]. Script body is
                           NOT rendered (AppleScript injection guard); user
                           input flows only through the optional ``argv``
                           list, each item rendered. Mirrors the
                           ``_build_paste_argv`` argv-binding pattern.
            poll_window  — repeats ``aerospace list-windows --all`` until a
                           row matches (app + optional title substring),
                           writes the window-id to ``capture_var``. Times out
                           after ``timeout_s`` (default 8s).

        Errors propagate to the standard error_template / fallback layer.
        ``FileNotFoundError`` covers AeroSpace not installed; ``_PollTimeout``
        covers app launched but window never became visible (e.g. minimized
        on launch, or sandboxed app rejecting AX).
        """
        action = skill["action"]
        response_cfg = skill.get("response", {})
        error_template = response_cfg.get("error_template")

        action_id = str(params.get("action_id") or "").strip()
        registry = action.get("registry", {})
        if action_id not in registry:
            msg = (
                f"Unknown mac_gui action_id: {action_id!r} "
                f"(known: {sorted(registry)})"
            )
            LOGGER.warning(msg)
            return error_template if error_template else msg

        # Resolve display alias before any step renders.
        display_aliases = action.get("display_aliases", {}) or {}
        if "display" in params and params["display"] in display_aliases:
            params["display"] = display_aliases[params["display"]]

        op = registry[action_id]
        try:
            for step in op.get("steps", []):
                self._run_aerospace_step(step, params)
        except FileNotFoundError as exc:
            LOGGER.warning("aerospace_op: missing binary: %s", exc)
            return (
                error_template
                if error_template
                else f"依赖缺失: {exc}"
            )
        except subprocess.TimeoutExpired:
            LOGGER.warning("aerospace_op: subprocess timeout for %s", action_id)
            return error_template if error_template else "操作超时"
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or b"")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", "replace")
            stderr = stderr.strip()
            LOGGER.warning(
                "aerospace_op: subprocess failed (exit=%s): %s",
                exc.returncode,
                stderr,
            )
            return (
                error_template
                if error_template
                else f"命令失败: {stderr[:80]}"
            )
        except _PollTimeout as exc:
            LOGGER.warning("aerospace_op: %s", exc)
            return error_template if error_template else str(exc)
        except Exception:
            LOGGER.exception("aerospace_op: unexpected step error for %s", action_id)
            return error_template if error_template else _FALLBACK_ERROR

        # Per-action response → outer skill response template.
        op_response_tpl = op.get("response", "完成")
        try:
            result_str = self._render(op_response_tpl, params)
        except Exception:
            LOGGER.exception("aerospace_op: response render failed for %s", action_id)
            return error_template if error_template else _FALLBACK_ERROR

        outer_tpl = response_cfg.get("template", "{{ result }}")
        try:
            return self._render(outer_tpl, {**params, "result": result_str})
        except Exception:
            LOGGER.exception(
                "aerospace_op: outer response render failed for %s", action_id
            )
            return error_template if error_template else _FALLBACK_ERROR

    def _run_aerospace_step(self, step: dict, params: dict) -> None:
        """Dispatch a single aerospace_op step. Mutates *params* on capture."""
        stype = step.get("type", "")
        if stype == "shell":
            self._step_shell(step, params)
            return
        if stype == "applescript":
            self._step_applescript(step, params)
            return
        if stype == "poll_window":
            self._step_poll_window(step, params)
            return
        msg = f"Unsupported aerospace step type: {stype!r}"
        LOGGER.warning(msg)
        raise ValueError(msg)

    def _step_shell(self, step: dict, params: dict) -> None:
        """Run ``subprocess.run(cmd, check=True)`` with each argv part rendered.

        ``check=True`` propagates non-zero exit as ``CalledProcessError`` to
        the outer handler. ``shell=False`` (argv list) keeps user-rendered
        values shell-inert.
        """
        cmd_template = step.get("cmd")
        if not isinstance(cmd_template, list) or not cmd_template:
            raise ValueError(f"shell step requires non-empty cmd list, got {cmd_template!r}")
        cmd = [self._render(str(part), params) for part in cmd_template]
        timeout_s = float(step.get("timeout_s", _AEROSPACE_STEP_TIMEOUT_S))
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=True,
        )
        capture_var = step.get("capture_var")
        if capture_var:
            params[str(capture_var)] = result.stdout.strip()

    def _step_applescript(self, step: dict, params: dict) -> None:
        """Run ``osascript -e <script> [argv...]``.

        Script body is treated as a literal — never rendered through Jinja2 —
        so user-supplied params cannot inject AppleScript. The ``argv`` list
        IS rendered, and each rendered string is passed as a positional
        argument to osascript (accessed inside AppleScript via
        ``on run argv ... item N of argv ... end run``). Same defense as
        ``_build_paste_argv``.
        """
        script = step.get("script")
        if not isinstance(script, str) or not script:
            raise ValueError("applescript step requires non-empty script string")
        argv_template = step.get("argv", []) or []
        rendered_argv = [self._render(str(a), params) for a in argv_template]
        timeout_s = float(step.get("timeout_s", _AEROSPACE_STEP_TIMEOUT_S))
        subprocess.run(
            ["osascript", "-e", script, *rendered_argv],
            capture_output=True,
            check=True,
            timeout=timeout_s,
        )

    def _step_poll_window(self, step: dict, params: dict) -> None:
        """Poll ``aerospace list-windows --all`` until a matching window appears.

        Match: ``app-name == match_app`` AND (if provided)
        ``match_title_contains in window-title``. On hit, the window-id is
        written to ``params[capture_var]``. On timeout, raises ``_PollTimeout``.

        Output is parsed as tab-separated rows from the
        ``%{window-id}\t%{app-name}\t%{window-title}`` format string —
        avoids JSON-parsing surprises across AeroSpace versions and
        sidesteps tab-in-title edge cases by limiting splits to 2.
        """
        match_app = self._render(str(step.get("match_app", "")), params)
        match_title = self._render(str(step.get("match_title_contains", "")), params)
        capture_var = step.get("capture_var")
        if not capture_var:
            raise ValueError("poll_window step requires capture_var")
        if not match_app:
            raise ValueError("poll_window step requires match_app")

        timeout_s = float(step.get("timeout_s", _AEROSPACE_POLL_TIMEOUT_S))
        interval_s = float(step.get("interval_s", _AEROSPACE_POLL_INTERVAL_S))

        deadline = time.monotonic() + timeout_s
        while True:
            try:
                result = subprocess.run(
                    [
                        "aerospace", "list-windows", "--all",
                        "--format", _AEROSPACE_LIST_FORMAT,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=_AEROSPACE_LIST_TIMEOUT_S,
                    check=True,
                )
                stdout = result.stdout
            except subprocess.CalledProcessError:
                # AeroSpace daemon may be transiently unavailable; keep
                # polling until our own timeout fires.
                stdout = ""
            for line in stdout.splitlines():
                parts = line.split("\t", 2)
                if len(parts) < 2:
                    continue
                wid, app = parts[0].strip(), parts[1].strip()
                title = parts[2] if len(parts) >= 3 else ""
                if app != match_app:
                    continue
                if match_title and match_title not in title:
                    continue
                params[str(capture_var)] = wid
                return
            if time.monotonic() >= deadline:
                raise _PollTimeout(
                    f"窗口未出现 ({timeout_s:g}s 内): "
                    f"app={match_app!r} title~{match_title!r}"
                )
            time.sleep(interval_s)

    def _resolve_zellij_pane(self, session: str, pane_title: str) -> tuple[str, str]:
        """Find the pane_id and cwd of the named pane in a zellij session.

        Calls ``zellij --session <s> action list-panes --json --command``
        and matches a non-plugin pane whose ``title`` equals ``pane_title``.
        Returns ``(pane_id, pane_cwd)`` where ``pane_id`` is in the form
        ``terminal_<n>`` accepted by zellij CLI.

        Raises:
            _PaneNotFound       — no matching pane in the session
            FileNotFoundError   — zellij not on PATH
            subprocess.TimeoutExpired
            subprocess.CalledProcessError — zellij CLI errored
        """
        result = subprocess.run(
            ["zellij", "--session", session, "action", "list-panes", "--json", "--command"],
            capture_output=True,
            text=True,
            timeout=_ZELLIJ_TIMEOUT_S,
            check=True,
        )
        try:
            panes = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise _PaneNotFound(f"无法解析 zellij list-panes 输出: {exc}") from exc

        if not isinstance(panes, list):
            raise _PaneNotFound("zellij list-panes 返回非列表")

        for pane in panes:
            if not isinstance(pane, dict):
                continue
            if pane.get("is_plugin"):
                continue
            if pane.get("title") != pane_title:
                continue
            pane_num = pane.get("id")
            if pane_num is None:
                continue
            cwd = pane.get("pane_cwd", "") or ""
            return f"terminal_{pane_num}", cwd

        raise _PaneNotFound(f"未找到 pane: title={pane_title!r} session={session!r}")

    @staticmethod
    def _format_cc_state_with_candidates(
        pane_title: str,
        ui_tail: str,
        candidates: list[cc_jsonl_reader.CCCandidate],
    ) -> str:
        """Render viewport + N candidates + matching instructions for the chat LLM.

        The chat LLM reads this as the cc_show tool result and is
        expected to:
            1. Match ``ui_tail`` content against each candidate's
               ``last_user_prompt`` / ``last_assistant_text`` / tool call
               summaries.
            2. Use the matched candidate as the cc conversation source
               for whatever the user asked.
            3. Fall back to viewport-only narration if no candidate
               matches (cc just started, idle too long, etc.).
        """
        parts: list[str] = [
            f"## cc Pane {pane_title!r} 终端状态\n{ui_tail}"
        ]

        if not candidates:
            parts.append(
                "## 该项目没找到 cc 对话日志候选\n"
                "（可能 cc 刚起且还没写 jsonl，或者 pane 的 cwd 不是任何 cc "
                "项目目录。仅用 viewport 信息回答即可。）"
            )
        else:
            now = time.time()
            cand_lines = [
                f"## 该项目最近活跃的 cc 对话日志候选（前 {len(candidates)} 个，按 mtime 倒序）"
            ]
            for idx, cand in enumerate(candidates, 1):
                cand_lines.append(_render_cc_candidate(idx, cand, now))
            parts.append("\n\n".join(cand_lines))

        parts.append(
            "## 给 LLM 的匹配指引\n"
            "viewport 显示的是这个 pane 的当前 UI 状态。请把 viewport 内容跟上面候选的 "
            "user / assistant / tool 比对，**挑出最一致的那一条候选**作为这个 pane 对应的 "
            "cc 对话源，回答用户时综合 viewport + 匹配候选。如果 viewport 跟所有候选都对不上"
            "（cc 刚起或长时间 idle），仅基于 viewport 信息回答。"
        )
        return "\n\n".join(parts)

    @staticmethod
    def _build_paste_argv(target: str) -> list[str]:
        """Return osascript argv for the requested target mode.

        - empty → just the fixed paste keystroke
        - prev keyword → Cmd+Tab + delay + paste
        - app name → resolve to canonical case via installed-apps scan,
          then activate-via-argv-binding + delay + paste with the canonical
          name as a positional arg (never interpolated into -e fragments)
        """
        if not target:
            return ["osascript", "-e", _APPLESCRIPT_PASTE]
        if target.lower() in _PREV_KEYWORDS:
            return [
                "osascript",
                "-e", _APPLESCRIPT_CMDTAB,
                "-e", _APPLESCRIPT_DELAY,
                "-e", _APPLESCRIPT_PASTE,
            ]
        canonical = _resolve_app_name(target)
        return [
            "osascript",
            "-e", "on run argv",
            "-e", "tell application (item 1 of argv) to activate",
            "-e", _APPLESCRIPT_DELAY,
            "-e", _APPLESCRIPT_PASTE,
            "-e", "end run",
            canonical,
        ]

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
        message = self._render(template_str, context)
        if response_cfg.get("envelope"):
            return make_tool_result(
                str(response_cfg.get("status", SUCCESS)),
                message,
            )
        return message
