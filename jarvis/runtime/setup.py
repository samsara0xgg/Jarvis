"""First-run setup: what the desktop's onboarding pages read and save.

The user's name goes to the memory.db profile, the assistant's name and the
``onboarded`` marker to ``settings.yaml``, API keys to the login Keychain
(only after they pass their checks). Keys are also checked once at boot, so
``GET /inherent/setup`` can say which ones are missing or refused, and why.
Everything that needs a restart takes effect at ``done``, which restarts
the daemon.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

import yaml

from jarvis.decision.llm import failure_reason
from jarvis.deployment import save_key
from jarvis.runtime.settings import SETUP_VOICES, VOICES
from jarvis.shared import lang
from jarvis.state.memory_db import set_user_name, user_name

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

LOGGER = logging.getLogger(__name__)
KEY_ENVS: Final[dict[str, str]] = {
    "openai": "OPENAI_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "tavily": "TAVILY_API_KEY",
}
_TIMEOUT_S: Final[float] = 20.0
_MAX_NAME: Final[int] = 40
_MAX_PREVIEW: Final[int] = 200
_MAX_KEY: Final[int] = 512
# Reasons that say the key itself is no good (not the network or a busy server).
_BAD_KEY: Final[frozenset[str]] = frozenset({"unauthorized", "quota", "model_denied"})
_FIRST_LINE_PROMPT: Final[str] = (
    "You are {assistant}, a personal assistant that lives on the user's Mac. {who}"
    "In one short, warm sentence in {language}, greet the user{by_name}, say this API key"
    " works and you are ready. Reply with that sentence only."
)
_TAVILY_SEARCH: Final[str] = "https://api.tavily.com/search"


class SetupError(ValueError):
    """A request the setup routes answer with 400."""


def write_setting(settings_path: Path, key: str, value: str) -> None:
    """Set the top-level ``key:`` line of ``settings.yaml``; the rest stays as written."""
    text = settings_path.read_text(encoding="utf-8") if settings_path.is_file() else ""
    # Plain when YAML reads it back unchanged (``en``, ``Nova``), else quoted (``No``, ``a: b``).
    try:
        plain = yaml.safe_load(value) == value
    except yaml.YAMLError:
        plain = False
    line = f"{key}: {value if plain else json.dumps(value, ensure_ascii=False)}\n"
    text, found = re.subn(rf"(?m)^{re.escape(key)}:.*(?:\n|$)", lambda _m: line, text)
    if not found:
        text += ("\n" if text and not text.endswith("\n") else "") + line
    staged = settings_path.with_suffix(".yaml.tmp")
    staged.write_text(text, encoding="utf-8")
    staged.replace(settings_path)


def _clean_name(value: object) -> str:
    """A name as typed: one line, 1 to 40 characters."""
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > _MAX_NAME:
        msg = f"a name is 1 to {_MAX_NAME} characters"
        raise SetupError(msg)
    if any(ord(char) < 32 for char in value):  # noqa: PLR2004 — C0 control characters.
        msg = "a name is one line"
        raise SetupError(msg)
    return value.strip()


def _check(
    check_id: str, exc: BaseException | None = None, reason: str | None = None,
) -> dict[str, Any]:
    """One row of a key check: passed, or why not in the user's language."""
    why = reason or (failure_reason(exc) if exc is not None else None)
    if why is None:
        return {"id": check_id, "ok": True}
    return {"id": check_id, "ok": False, "reason": why, "detail": lang.t(f"failure.{why}")}


def _post_json(url: str, key: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """POST ``body`` with the key as a bearer token; the parsed answer."""
    request = urllib.request.Request(  # noqa: S310 — fixed https endpoints.
        url,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as reply:  # noqa: S310
        answer = json.load(reply)
    return answer if isinstance(answer, dict) else {}


def _http_reason(exc: BaseException) -> str:
    """Why a plain HTTP call failed, in :func:`failure_reason`'s words."""
    if isinstance(exc, urllib.error.HTTPError):
        return {401: "unauthorized", 403: "unauthorized", 429: "rate_limited"}.get(
            exc.code, "quota" if exc.code in (402, 432, 433) else "error",  # Tavily's plan limits.
        )
    if isinstance(exc, TimeoutError) or "timed out" in str(exc):
        return "timeout"
    if isinstance(exc, urllib.error.URLError | OSError):
        return "network"
    return "error"


def _minimax_reason(status: object) -> str | None:
    """MiniMax answers 200 with ``base_resp.status_code``; 0 is success."""
    if status == 0:
        return None
    return {1004: "unauthorized", 2049: "unauthorized", 1008: "quota", 1002: "rate_limited"}.get(
        status if isinstance(status, int) else -1, "error",
    )


class Setup:
    """The onboarding pages' view of this install; this process is the only writer."""

    def __init__(  # noqa: PLR0913 — the paths and services it reads and writes, all required.
        self,
        *,
        root: Path,
        settings_path: Path,
        memory_path: Path,
        config: Mapping[str, Any],
        tts_endpoint: str,
        tts_model: str,
        restart: Callable[[], None] | None,
    ) -> None:
        """Bind what setup reads and writes; ``restart`` is None when launchd cannot respawn us."""
        self._root = root
        self._settings_path = settings_path
        self._memory_path = memory_path
        self._config = config
        self._tts_endpoint = tts_endpoint.rstrip("/")
        self._tts_model = tts_model
        self._restart = restart
        # provider -> the last check's key-level reason (None = passed).
        self._refused: dict[str, str | None] = {}

    # --- reads ---------------------------------------------------------------

    def _settings(self) -> dict[str, Any]:
        if not self._settings_path.is_file():
            return {}
        raw = yaml.safe_load(self._settings_path.read_text(encoding="utf-8")) or {}
        return raw if isinstance(raw, dict) else {}

    def _key_state(self, provider: str) -> str:
        if not os.environ.get(KEY_ENVS[provider]):
            return "missing"
        return "bad" if self._refused.get(provider) in _BAD_KEY else "ok"

    def voices(self) -> list[dict[str, str]]:
        """The five voices setup offers in the current language."""
        return [
            {"id": one, "label": lang.t(f"voice.{one}"), "note": lang.t(f"voice.{one}.note")}
            for one in SETUP_VOICES[lang.language()]
        ]

    def _features(self) -> dict[str, dict[str, Any]]:
        """What works with the keys there are, and why not when it does not."""
        keys = {provider: self._key_state(provider) for provider in KEY_ENVS}
        live = self._config.get("realtime", {}).get("gpt_live", {}).get("enabled") is True

        def feature(on: bool, reason: str | None) -> dict[str, Any]:  # noqa: FBT001
            return {"on": on, "reason": None if on else reason}

        search = keys["tavily"] == "ok" or bool(os.environ.get("EXA_API_KEY"))
        return {
            "chat": feature(keys["openai"] == "ok", f"openai_{keys['openai']}"),
            "reply_voice": feature(keys["minimax"] == "ok", f"minimax_{keys['minimax']}"),
            "live_voice": feature(
                live and keys["openai"] == "ok", "off" if not live else f"openai_{keys['openai']}",
            ),
            "web_search": feature(search, f"tavily_{keys['tavily']}"),
        }

    def read(self) -> dict[str, Any]:
        """``GET /inherent/setup``: whether setup has run, what it saved, what works."""
        settings = self._settings()
        assistant = settings.get("assistant_name") or self._config.get("assistant_name")
        return {
            "first_run": not settings.get("onboarded"),
            "name": user_name(self._memory_path),
            "assistant_name": assistant if isinstance(assistant, str) and assistant else "Jarvis",
            "language": lang.language(),
            "keys": {provider: self._key_state(provider) for provider in KEY_ENVS},
            "voices": self.voices(),
            "features": self._features(),
        }

    # --- writes --------------------------------------------------------------

    def save_names(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """``POST /inherent/setup/name``: the user's name and/or the assistant's."""
        if body.get("name") is None and body.get("assistant_name") is None:
            msg = "send name, assistant_name or both"
            raise SetupError(msg)
        if body.get("name") is not None:
            set_user_name(self._memory_path, _clean_name(body["name"]))
        if body.get("assistant_name") is not None:
            assistant = _clean_name(body["assistant_name"])
            write_setting(self._settings_path, "assistant_name", assistant)
        return self.read()

    def check_key(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """``POST /inherent/setup/key``: test a pasted key, keep it only when it works."""
        provider, key = body.get("provider"), body.get("key")
        if provider not in KEY_ENVS:
            msg = f"provider must be one of {', '.join(KEY_ENVS)}"
            raise SetupError(msg)
        if not isinstance(key, str) or not key.strip() or len(key.strip()) > _MAX_KEY:
            msg = "key must be a non-empty string"
            raise SetupError(msg)
        key = key.strip()
        if provider == "openai":
            name = body.get("name") if isinstance(body.get("name"), str) else None
            assistant = body.get("assistant_name")
            answer = self._check_openai(
                key,
                name=name,
                assistant=assistant if isinstance(assistant, str) and assistant else None,
            )
        elif provider == "minimax":
            answer = self._check_minimax(key)
        else:
            answer = self._check_tavily(key)
        # Only a key the service accepted counts: connect (and, for OpenAI, a real answer).
        must_pass = ("connect", "chat") if provider == "openai" else ("connect",)
        refused = [
            row["reason"] for row in answer["checks"] if row["id"] in must_pass and not row["ok"]
        ]
        answer["ok"] = not refused
        self._refused[provider] = refused[0] if refused else None
        if answer["ok"]:
            save_key(self._root, KEY_ENVS[provider], key)
        return answer

    def preview(self, body: Mapping[str, Any]) -> bytes:
        """``POST /inherent/setup/voice-preview``: one line in ``voice_id``, as MP3 bytes."""
        voice = body.get("voice_id")
        if voice not in VOICES:
            msg = "voice_id is not one Jarvis offers"
            raise SetupError(msg)
        text = body.get("text")
        if text is None:
            text = lang.t("setup.preview", assistant=self.read()["assistant_name"])
        if not isinstance(text, str) or not text.strip() or len(text) > _MAX_PREVIEW:
            msg = f"text is 1 to {_MAX_PREVIEW} characters"
            raise SetupError(msg)
        key = os.environ.get(KEY_ENVS["minimax"])
        if not key:
            msg = "save a MiniMax key first"
            raise SetupError(msg)
        reason, audio = self._speak(key, str(voice), text)
        if reason is not None:
            msg = f"MiniMax could not speak the preview: {reason}"
            raise RuntimeError(msg)
        return audio

    def _speak(self, key: str, voice: str, text: str) -> tuple[str | None, bytes]:
        """MiniMax's one-shot synthesis on the endpoint replies use: (why not, MP3 bytes)."""
        answer = _post_json(f"{self._tts_endpoint}/v1/t2a_v2", key, {
            "model": self._tts_model,
            "text": text,
            "stream": False,
            "voice_setting": {"voice_id": voice, "speed": 1, "vol": 1, "pitch": 0},
            "audio_setting": {
                "sample_rate": 32000, "bitrate": 128000, "format": "mp3", "channel": 1,
            },
        })
        audio = (answer.get("data") or {}).get("audio")
        reason = _minimax_reason((answer.get("base_resp") or {}).get("status_code"))
        if reason is not None:
            return reason, b""
        if not isinstance(audio, str) or not audio:
            return "error", b""
        return None, bytes.fromhex(audio)

    def done(self) -> dict[str, Any]:
        """``POST /inherent/setup/done``: setup will not show again; restart to take it all in."""
        write_setting(self._settings_path, "onboarded", datetime.now().astimezone().isoformat())
        if self._restart is not None:
            self._restart()
        return {"ok": True, "restarting": self._restart is not None}

    # --- the checks ----------------------------------------------------------

    def _models(self) -> tuple[Mapping[str, Any], set[str], str | None]:
        """The conversation preset, the background models and the realtime model."""
        llm = self._config.get("llm", {})
        presets = llm.get("presets", {})
        chat = presets.get(llm.get("default_preset"), {})
        background = {
            presets.get(preset, {}).get("model")
            for preset in (
                self._config.get("work_state", {}).get("preset"),
                self._config.get("daily_report", {}).get("preset"),
            )
        } - {None}
        live = self._config.get("realtime", {}).get("gpt_live", {}).get("model")
        return chat, background, live

    def _check_openai(
        self, key: str, *, name: str | None, assistant: str | None, chat_call: bool = True,
    ) -> dict[str, Any]:
        """Connect (list models), chat (one real answer), deep and live (model access)."""
        from openai import OpenAI  # noqa: PLC0415 — the SDK loads only when a key is checked.

        chat, background, live = self._models()
        client = OpenAI(
            api_key=key, base_url=chat.get("base_url") or None, timeout=_TIMEOUT_S, max_retries=0,
        )
        try:
            available = {model.id for model in client.models.list()}
        except Exception as exc:  # noqa: BLE001 — every failure is a reason on every row.
            reason = failure_reason(exc)
            rows = ("connect", "chat", "deep", "live")
            return {"checks": [_check(one, reason=reason) for one in rows]}
        checks = [_check("connect")]
        answer: dict[str, Any] = {}
        if chat_call:
            assistant = assistant or self.read()["assistant_name"]
            prompt = _FIRST_LINE_PROMPT.format(
                assistant=assistant,
                who=f"The user's name is {name}. " if name else "",
                language=lang.language_name(),
                by_name=" by name" if name else "",
            )
            effort = chat.get("reasoning_effort")
            started = time.monotonic()
            try:
                reply = client.chat.completions.create(
                    model=str(chat.get("model")),
                    messages=[{"role": "user", "content": prompt}],
                    max_completion_tokens=200,
                    **({"reasoning_effort": effort} if effort else {}),
                )
                answer["first_line"] = (reply.choices[0].message.content or "").strip()
                answer["seconds"] = round(time.monotonic() - started, 1)
                checks.append(_check("chat"))
            except Exception as exc:  # noqa: BLE001 — shown as the row's reason.
                checks.append(_check("chat", exc))
        denied = "model_denied"
        checks.append(_check("deep", reason=None if background <= available else denied))
        checks.append(_check("live", reason=None if live in available else denied))
        return {**answer, "checks": checks}

    def _check_minimax(self, key: str) -> dict[str, Any]:
        """Connect: MiniMax speaks one word for this key (two characters of TTS)."""
        try:
            reason, _audio = self._speak(key, SETUP_VOICES[lang.language()][0], "OK")
        except Exception as exc:  # noqa: BLE001 — shown as the row's reason.
            return {"checks": [_check("connect", reason=_http_reason(exc))], "voices": []}
        voices = [] if reason else self.voices()
        return {"checks": [_check("connect", reason=reason)], "voices": voices}

    def _check_tavily(self, key: str) -> dict[str, Any]:
        """connect: one one-result search (it uses one Tavily credit)."""
        try:
            _post_json(_TAVILY_SEARCH, key, {"query": "Jarvis", "max_results": 1})
        except Exception as exc:  # noqa: BLE001 — shown as the row's reason.
            return {"checks": [_check("connect", reason=_http_reason(exc))]}
        return {"checks": [_check("connect")]}

    def check_at_boot(self) -> None:
        """Check the saved OpenAI and MiniMax keys (no tokens, one word of TTS); log what fails."""
        for provider in ("openai", "minimax"):
            key = os.environ.get(KEY_ENVS[provider])
            if not key:
                LOGGER.warning("setup: no %s key; %s", provider, KEY_ENVS[provider])
                continue
            if provider == "openai":
                rows = self._check_openai(key, name=None, assistant=None, chat_call=False)["checks"]
            else:
                rows = self._check_minimax(key)["checks"]
            failed = [row for row in rows if not row["ok"]]
            self._refused[provider] = next(
                (row["reason"] for row in failed if row["id"] == "connect"), None,
            )
            for row in failed:
                LOGGER.warning(
                    "setup: %s key check %s failed: %s", provider, row["id"], row["reason"],
                )
