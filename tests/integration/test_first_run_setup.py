"""First-run setup: the routes the onboarding pages call, and where what they save lands.

Each check asserts what a caller observes: the route's status and answer, the
memory.db profile line, the user's settings.yaml, the next boot's config, the
login Keychain item for a throwaway runtime root, and the reason a failed
model call is given.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from contextlib import closing
from typing import TYPE_CHECKING, Any

import httpx
import openai
import pytest
from fastapi.testclient import TestClient

from jarvis.decision.llm import MissingAPIKeyError, failure_reason
from jarvis.deployment import load_env_file, read_keys, save_key
from jarvis.runtime import _load_full_config
from jarvis.runtime import setup as runtime_setup
from jarvis.runtime.settings import SETUP_VOICES
from jarvis.runtime.setup import KEY_ENVS, Setup
from jarvis.shared import lang
from jarvis.state.memory_db import open_memory_db
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app

if TYPE_CHECKING:
    from pathlib import Path

CONFIG: dict[str, Any] = {
    "llm": {"default_preset": "luna", "presets": {"luna": {"model": "gpt-5.6-luna"}}},
    "realtime": {"gpt_live": {"enabled": True, "model": "gpt-live-1"}},
}


def _setup(root: Path) -> Setup:
    return Setup(
        root=root,
        settings_path=root / "settings.yaml",
        memory_path=root / "memory.db",
        config=CONFIG,
        tts_endpoint="https://tts.invalid",
        tts_model="speech-2.8-turbo",
        restart=None,
    )


def _client(setup: Setup) -> TestClient:
    return TestClient(create_app(InherentDeps(
        submit_callable=lambda _text: None, broadcaster=InherentBroadcaster(), setup=setup,
    )))


@pytest.fixture(autouse=True)
def _no_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stranger's machine: none of the setup keys, fixed text in Chinese."""
    for env in (*KEY_ENVS.values(), "EXA_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    lang.set_language("zh")


def test_a_fresh_install_is_a_first_run_until_done(tmp_path: Path) -> None:
    """Names land in the profile and settings.yaml, the next boot uses the name; done ends it."""
    client = _client(_setup(tmp_path))
    fresh = client.get("/inherent/setup").json()
    assert fresh["first_run"] is True
    assert (fresh["name"], fresh["assistant_name"], fresh["language"]) == (None, "Jarvis", "zh")
    assert fresh["keys"] == {"openai": "missing", "minimax": "missing", "tavily": "missing"}
    assert fresh["features"]["reply_voice"] == {"on": False, "reason": "minimax_missing"}
    assert fresh["features"]["live_voice"] == {"on": False, "reason": "openai_missing"}
    assert [voice["id"] for voice in fresh["voices"]] == list(SETUP_VOICES["zh"])
    assert fresh["voices"][0] == {
        "id": "Chinese (Mandarin)_Warm_Bestie", "label": "暖心闺蜜", "note": "温暖，清楚",  # noqa: RUF001
    }
    # In English, Allen's pick leads the four English voices he kept.
    lang.set_language("en")
    assert [voice["label"] for voice in client.get("/inherent/setup").json()["voices"]] == [
        "Warm Bestie", "Radiant Girl", "Calm Woman", "Friendly Guy", "Trustworthy Man",
    ]
    lang.set_language("zh")

    named = client.post("/inherent/setup/name", json={"name": "Ada", "assistant_name": "No"})
    assert named.status_code == 200
    assert (named.json()["name"], named.json()["assistant_name"]) == ("Ada", "No")
    with closing(open_memory_db(tmp_path / "memory.db")) as conn:
        rows = conn.execute("SELECT id, text FROM profile").fetchall()
    assert rows == [("profile-name", "The user's name is Ada.")]
    # "No" would read back as false unquoted; the next boot renders it into {assistant}.
    assert (tmp_path / "settings.yaml").read_text(encoding="utf-8") == 'assistant_name: "No"\n'
    shipped = tmp_path / "jarvis.yaml"
    shipped.write_text('persona: "You are {assistant}."\n', encoding="utf-8")
    assert _load_full_config(shipped, tmp_path / "settings.yaml")["persona"] == "You are No."

    renamed = client.post("/inherent/setup/name", json={"name": "Ada Lovelace"}).json()
    assert (renamed["name"], renamed["assistant_name"]) == ("Ada Lovelace", "No")

    assert client.post("/inherent/setup/done").json() == {"ok": True, "restarting": False}
    assert client.get("/inherent/setup").json()["first_run"] is False
    assert "onboarded: " in (tmp_path / "settings.yaml").read_text(encoding="utf-8")


def test_bad_requests_are_400_and_never_echo_the_key(tmp_path: Path) -> None:
    """Unknown provider, an over-long name, an unknown voice, no body: 400, nothing saved."""
    client = _client(_setup(tmp_path))
    pasted = "sk-proj-SECRET-do-not-echo"
    for route, body in (
        ("/inherent/setup/key", {"provider": "anthropic", "key": pasted}),
        ("/inherent/setup/key", {"provider": "openai", "key": ""}),
        ("/inherent/setup/name", {"name": "x" * 41}),
        ("/inherent/setup/name", {"name": "two\nlines"}),
        ("/inherent/setup/name", {}),
        ("/inherent/setup/voice-preview", {"voice_id": "Robot", "text": pasted}),
    ):
        answer = client.post(route, json=body)
        assert (route, answer.status_code) == (route, 400)
        assert pasted not in answer.text
    assert client.post("/inherent/setup/key", content=b"[1]").status_code == 400
    assert not (tmp_path / "settings.yaml").exists()
    assert not (tmp_path / "memory.db").exists()


def test_a_key_is_kept_only_when_connect_and_chat_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused key keeps nothing; a working one is saved even if a background model is denied."""
    saved: list[tuple[str, str]] = []

    def _save(_root: Path, name: str, key: str) -> None:
        saved.append((name, key))
        monkeypatch.setenv(name, key)

    rows: dict[str, list[dict[str, Any]]] = {
        "refused": [{"id": "connect", "ok": True}, {"id": "chat", "ok": False, "reason": "quota"}],
        "works": [
            {"id": "connect", "ok": True}, {"id": "chat", "ok": True},
            {"id": "deep", "ok": False, "reason": "model_denied"}, {"id": "live", "ok": True},
        ],
    }
    outcome = "refused"
    monkeypatch.setattr(runtime_setup, "save_key", _save)
    monkeypatch.setattr(
        Setup, "_check_openai", lambda *_a, **_k: {"checks": rows[outcome], "first_line": "hi"},
    )
    client = _client(_setup(tmp_path))

    refused = client.post("/inherent/setup/key", json={"provider": "openai", "key": "sk-a"})
    assert refused.json()["ok"] is False
    assert saved == []
    assert client.get("/inherent/setup").json()["keys"]["openai"] == "missing"

    outcome = "works"
    works = client.post("/inherent/setup/key", json={"provider": "openai", "key": " sk-b "}).json()
    assert (works["ok"], works["first_line"]) == (True, "hi")
    assert saved == [("OPENAI_API_KEY", "sk-b")]
    status = client.get("/inherent/setup").json()
    assert status["keys"]["openai"] == "ok"
    assert status["features"]["live_voice"] == {"on": True, "reason": None}


def _status_error(cls: type[openai.APIStatusError], status: int, body: object) -> BaseException:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    return cls("refused", response=httpx.Response(status, request=request), body=body)


def test_a_failed_model_call_names_its_reason() -> None:
    """Each way a key or model call fails maps to the reason the desktop explains."""
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    wrapped = RuntimeError("drive_turn failed")
    wrapped.__cause__ = _status_error(openai.AuthenticationError, 401, None)
    cases: list[tuple[BaseException, str]] = [
        (MissingAPIKeyError("unset"), "missing_key"),
        (_status_error(openai.AuthenticationError, 401, None), "unauthorized"),
        (wrapped, "unauthorized"),
        (_status_error(openai.PermissionDeniedError, 403, None), "model_denied"),
        (_status_error(openai.NotFoundError, 404, None), "model_denied"),
        (_status_error(openai.RateLimitError, 429, {"code": "insufficient_quota"}), "quota"),
        (_status_error(openai.RateLimitError, 429, {"code": "rate_limited"}), "rate_limited"),
        (openai.APITimeoutError(request=request), "timeout"),
        (openai.APIConnectionError(request=request), "network"),
        (ValueError("anything else"), "error"),
    ]
    assert [failure_reason(exc) for exc, _ in cases] == [reason for _, reason in cases]
    for _, reason in cases:
        assert lang.t(f"failure.{reason}", lang="en")
        assert lang.t(f"failure.{reason}", lang="zh")


@pytest.mark.skipif(
    sys.platform != "darwin" or not shutil.which("security"), reason="macOS Keychain",
)
def test_keys_round_trip_through_the_keychain_for_their_root_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """save_key -> the root's Keychain item -> the next boot's environment, before the env file."""
    name = "JARVIS_SETUP_CHECK_KEY"
    monkeypatch.delenv(name, raising=False)
    (tmp_path / "env").write_text(f"{name}=from-the-plaintext-file\n", encoding="utf-8")
    try:
        save_key(tmp_path, name, "sk-from-keychain")
        assert os.environ[name] == "sk-from-keychain"
        assert read_keys(tmp_path) == {name: "sk-from-keychain"}
        assert read_keys(tmp_path / "other-root") == {}
        monkeypatch.delenv(name)
        assert load_env_file(tmp_path) == {name: "sk-from-keychain"}
        assert os.environ[name] == "sk-from-keychain"
    finally:
        subprocess.run(  # noqa: S603 — fixed macOS tool.
            ["security", "delete-generic-password", "-s", "Jarvis", "-a", str(tmp_path)],  # noqa: S607
            capture_output=True, check=False,
        )
    assert read_keys(tmp_path) == {}
