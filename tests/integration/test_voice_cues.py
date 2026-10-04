"""ADR 0152: her state block carries his tone and his wordless laughs.

A real DuplexVoiceSession and VoicePipeline on the scripted ingress (the soft-barge-in
rig) hear a laugh over her voice, which is dropped as a listening sound, then a HAPPY
sentence; the one-turn request builder then shows what the model is sent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jarvis.runtime import _live_lines
from jarvis.surface.voice_cues import VoiceCues
from tests.integration.test_backend_prompt_assembly import HISTORY, LINE, STATUS, _drive_one_turn
from tests.integration.test_soft_barge_in import LONG, SHORT, _Rig
from tests.integration.test_wave3_single_audio_ingress import _wait_until

if TYPE_CHECKING:
    from pathlib import Path


def _request(tmp_path: Path, cues: VoiceCues) -> str:
    (tmp_path / "turn").mkdir()
    _, messages = _drive_one_turn(
        tmp_path / "turn", HISTORY, live_context=_live_lines((cues.line,)),
    )
    content = messages[2]["content"]
    assert isinstance(content, str)
    return content


def test_a_laugh_over_her_and_a_happy_sentence_reach_the_next_request(tmp_path: Path) -> None:
    """The laugh is no turn (she goes on); the cues ride the next turn once, then clear."""
    cues = VoiceCues()
    rig = _Rig(tmp_path, "哈哈。", cues=cues)
    rig.asr.tags = {"哈哈。": ("HAPPY", "Laughter"), "你说得太好笑了。": ("HAPPY", "Speech")}
    try:
        rig.say(SHORT)
        assert rig.turns() == []
        assert rig.speaking
        rig.speaking = False
        rig.asr._texts.append("你说得太好笑了。")  # noqa: SLF001 - scripts the next clip
        rig.say(LONG)
        _wait_until(lambda: rig.turns() == ["你说得太好笑了。"])
    finally:
        rig.close()

    cue_line = (
        "Voice cues (background, rarely worth mentioning; from audio, may be wrong): "
        "he sounded happy; "
        "he laughed without words while you were speaking.\n"
    )
    assert _request(tmp_path, cues) == f"{STATUS}{cue_line}\n后天呢\n\n{LINE}"
    # Read once: the same state now adds nothing.
    assert cues.line() is None


def test_no_cues_add_nothing_and_a_noisy_label_is_not_one(tmp_path: Path) -> None:
    """Empty state, NEUTRAL speech and BGM leave the request as it was."""
    cues = VoiceCues()
    cues.heard("好的。", "NEUTRAL", "BGM")
    assert _request(tmp_path, cues) == f"{STATUS}\n后天呢\n\n{LINE}"
