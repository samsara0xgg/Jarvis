"""ADR 0153: the quiet level is one daemon-owned switch, set by fixed phrases or the controls wire.

The phrase table is the matcher's contract; the session rig says each phrase through the real
capture path (no conversation mode, no wake hit) and sees the level set, her fixed line asked for
and no turn written; the wire and the saved file carry the level across a restart.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from jarvis.state import quiet_mode
from jarvis.surface import voice_asr
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app
from jarvis.surface.voice_controls import VoiceControls
from tests.integration.test_answer_waits_for_allen import _AcceptingPipeline
from tests.integration.test_conversation_mode import _Session
from tests.integration.test_wave3_single_audio_ingress import _wait_until

if TYPE_CHECKING:
    from pathlib import Path

_PHRASES = [
    ("安静一点", "quiet"), ("安静一点。", "quiet"), ("Jarvis, 安静一点吧", "quiet"),
    ("安静模式", "quiet"), ("quiet mode", "quiet"), ("stay quiet", "quiet"),
    ("别弹了", "no-pop"), ("不弹了。", "no-pop"), ("no popups", "no-pop"),
    ("勿扰", "dnd"), ("勿扰模式", "dnd"), ("请勿打扰", "dnd"), ("do not disturb", "dnd"),
    ("恢复正常", "off"), ("关掉勿扰", "off"), ("关闭安静模式", "off"),
    ("turn off do not disturb", "off"), ("back to normal", "off"),
    # Parts of sentences, the old stop meaning of 安静, and other dismissals are not these.
    ("安静", None), ("安静一下", None), ("我想安静一点工作", None), ("别弹了, 好吗", None),
    ("退下", None), ("帮我把勿扰关掉", None), ("今天勿扰模式开了吗", None),
]


@pytest.mark.parametrize(("said", "level"), _PHRASES)
def test_the_phrase_table(said: str, level: str | None) -> None:
    """Only the whole sentence sets a level, and a bare 安静 keeps its stop meaning."""
    assert voice_asr.quiet_command(said) == level


_SAID = [("勿扰", "dnd"), ("安静一点", "quiet"), ("恢复正常", "off")]


@pytest.mark.parametrize(("said", "level"), _SAID)
def test_a_phrase_sets_the_level_and_is_no_turn(
    monkeypatch: pytest.MonkeyPatch, said: str, level: str,
) -> None:
    """Said with no conversation mode open: the level is set, her line asked for, no turn."""
    record: list[str] = []
    set_to: list[str] = []
    asked: list[tuple[str, str]] = []
    rig = _Session(
        monkeypatch, conversation=False, wake=True,
        pipeline=_AcceptingPipeline(record, said=said),
        set_quiet=set_to.append,
        answer_words=lambda _turn, reason, _text: asked.append((reason, said)),
    )
    try:
        rig.speak()
        _wait_until(lambda: bool(asked))
        assert set_to == [level]
        assert asked == [(voice_asr.QUIET_REASONS[level], said)]
        assert "utterance.received" not in record
    finally:
        rig.close()


def test_the_wire_sets_the_level_and_the_file_keeps_it(tmp_path: Path) -> None:
    """A POST sets and saves it; a bad word is refused; a new daemon starts where it was left."""
    path = tmp_path / "quiet-mode.json"
    assert quiet_mode.load(path) == "off"
    controls = VoiceControls(
        quiet=quiet_mode.load(path), on_quiet=lambda level: quiet_mode.save(path, level),
    )
    client = TestClient(create_app(InherentDeps(
        submit_callable=lambda _text: None, broadcaster=InherentBroadcaster(), controls=controls,
    )))
    assert client.post("/inherent/controls", json={}).json()["quiet"] == "off"
    assert client.post("/inherent/controls", json={"quiet": "dnd"}).json()["quiet"] == "dnd"
    assert client.post("/inherent/controls", json={"quiet": "loud"}).status_code == 422
    assert client.post("/inherent/controls", json={"mic_muted": True}).json()["quiet"] == "dnd"
    assert quiet_mode.load(path) == "dnd"
    assert VoiceControls(quiet=quiet_mode.load(path)).update()["quiet"] == "dnd"
    path.write_text("not json", encoding="utf-8")
    assert quiet_mode.load(path) == "off"
