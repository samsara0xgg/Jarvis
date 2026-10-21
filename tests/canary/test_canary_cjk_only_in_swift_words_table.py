"""Canary — Chinese in the iPhone app's Swift string literals lives only in the words table.

The phone words every fixed string from ``ios/Jarvis/App/Words.swift`` and its
``Words+<area>.swift`` siblings, in the language the brain's settings name (ADR 0325). Files
that held Chinese literals before the table existed are on ``_LEGACY`` and move into the table
one by one; the list only shrinks: a file not on it that gains a Chinese literal fails, and so
does a file on it that holds none any more. Code that reads his own words keeps Chinese beside
English and is listed in ``_MATCHERS`` instead, with its reason.

Canary house style: stdlib only, no import of the code under test.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

from tests.canary._helpers import repo_root

_CJK: Final = re.compile(r"[　-〿一-鿿＀-￯]")

_ROOTS: Final = ("ios/Jarvis", "ios/JarvisWidgets", "ios/JarvisShare")

_TABLE_DIR: Final = "ios/Jarvis/App"

# Code that reads what he said (the recognizer's zh lane, word lists for matching), keyed by file.
# A file that mixes such lists with screen words stays on _LEGACY until the words move out.
_MATCHERS: Final[dict[str, str]] = {}

# DEBUG-only made-up days whose place and event names stand in for what the brain would send.
_SAMPLES: Final[dict[str, str]] = {
    "ios/Jarvis/Sky/FootprintFixture.swift": "the footprint page's made-up day (places, event title)",
}

# Files that held a Chinese literal when the table began (sorted, one path per line). Take a file
# off this list in the commit that moves its words into the table.
_LEGACY: Final = """
ios/Jarvis/Attach/AttachCamera.swift
ios/Jarvis/Attach/AttachDebug.swift
ios/Jarvis/Attach/AttachField.swift
ios/Jarvis/Attach/AttachModel.swift
ios/Jarvis/Attach/AttachPanel.swift
ios/Jarvis/Attach/AttachStrip.swift
ios/Jarvis/Attach/AttachUpload.swift
ios/Jarvis/Attach/PictureDebug.swift
ios/Jarvis/Attach/PictureViewer.swift
ios/Jarvis/Attach/PictureViews.swift
ios/Jarvis/Brain/About.swift
ios/Jarvis/Brain/BrainClient.swift
ios/Jarvis/Brain/BrainLink.swift
ios/Jarvis/Brain/DayActions.swift
ios/Jarvis/Brain/EngineBrain.swift
ios/Jarvis/Brain/PlaceRename.swift
ios/Jarvis/Brain/PlaceTint.swift
ios/Jarvis/Brain/RealDay.swift
ios/Jarvis/Brain/Ride.swift
ios/Jarvis/Brain/TalkStream.swift
ios/Jarvis/Brain/TalkTopics.swift
ios/Jarvis/Brain/TurnStatus.swift
ios/Jarvis/Brain/Waiting.swift
ios/Jarvis/Lifelog/LifelogCoverage.swift
ios/Jarvis/Lifelog/LifelogIntents.swift
ios/Jarvis/Map/PlaceMapViews.swift
ios/Jarvis/Push/JarvisActivity.swift
ios/Jarvis/Push/PushNotifications.swift
ios/Jarvis/Push/RideActivityAttributes.swift
ios/Jarvis/Push/RideLiveActivity.swift
ios/Jarvis/Report/ReportIntent.swift
ios/Jarvis/Report/Reporter.swift
ios/Jarvis/Settings/SettingsComponents.swift
ios/Jarvis/Settings/SettingsModel.swift
ios/Jarvis/Settings/SettingsPages.swift
ios/Jarvis/Settings/SettingsSheet.swift
ios/Jarvis/Sky/DayTimeline.swift
ios/Jarvis/Sky/DayTimelineActions.swift
ios/Jarvis/Sky/DayTimelineArc.swift
ios/Jarvis/Sky/DayTimelineCommit.swift
ios/Jarvis/Sky/DayTimelineDetail.swift
ios/Jarvis/Sky/DayTimelineDoing.swift
ios/Jarvis/Sky/DayTimelineHeader.swift
ios/Jarvis/Sky/DayTimelineMarks.swift
ios/Jarvis/Sky/DayTimelineModel.swift
ios/Jarvis/Sky/DayTimelineOpen.swift
ios/Jarvis/Sky/DayTimelineRowViews.swift
ios/Jarvis/Sky/DayTimelineRows.swift
ios/Jarvis/Sky/DayTimelineSayWhat.swift
ios/Jarvis/Sky/DayTimelineStar.swift
ios/Jarvis/Sky/DayTimelineTalk.swift
ios/Jarvis/Sky/DayTimelineTidy.swift
ios/Jarvis/Sky/DayTimelineTop.swift
ios/Jarvis/Sky/PanelRules.swift
ios/Jarvis/Sky/Reveal.swift
ios/Jarvis/Sky/SkyData.swift
ios/Jarvis/Sky/SkyMath.swift
ios/Jarvis/Sky/StarBrain.swift
ios/Jarvis/Sky/StarCards.swift
ios/Jarvis/Sky/StarChartScreen.swift
ios/Jarvis/Sky/StarFlow.swift
ios/Jarvis/Sky/StarPanel.swift
ios/Jarvis/Sky/StarRingScene.swift
ios/Jarvis/Sky/StarStage.swift
ios/Jarvis/Sky/StarStatus.swift
ios/Jarvis/Sky/StarTalk.swift
ios/Jarvis/Sky/StarTapback.swift
ios/Jarvis/Sky/StarTimelineScene.swift
ios/Jarvis/Sky/StarTune.swift
ios/Jarvis/Sky/StarUI.swift
ios/Jarvis/Sky/StarVoice.swift
ios/Jarvis/Voice/AskIntent.swift
ios/Jarvis/Voice/PhoneLink.swift
ios/Jarvis/Voice/TalkIntent.swift
ios/Jarvis/Voice/VoiceCore.swift
ios/Jarvis/Voice/VoiceEntry.swift
ios/Jarvis/Voice/VoiceLink.swift
ios/Jarvis/Voice/VoiceRecognizer.swift
ios/Jarvis/Voice/VoiceSession.swift
ios/Jarvis/Voice/VoiceStub.swift
ios/Jarvis/Voice/VoiceWire.swift
ios/JarvisShare/SharePayload.swift
ios/JarvisShare/ShareView.swift
ios/JarvisWidgets/JarvisActivityWidget.swift
ios/JarvisWidgets/JarvisWidgets.swift
ios/JarvisWidgets/LifelogControl.swift
ios/JarvisWidgets/RideActivityWidget.swift
""".split()


def swift_cjk_literal_lines(src: str) -> list[int]:
    """Lines of every string literal in Swift source that holds CJK; comments are skipped.

    Handles ``"..."``, ``\"\"\"`` blocks, ``#"..."#`` raw strings and ``\\(...)`` interpolations
    (which hold code, so string literals nested in them are scanned too).
    """
    found: list[int] = []
    n = len(src)

    def line(i: int) -> int:
        return src.count("\n", 0, i) + 1

    def code(i: int, in_interp: bool) -> int:
        """Scan code from i; return the index after the closing ``)`` of an interpolation."""
        depth = 0
        while i < n:
            c = src[i]
            if src.startswith("//", i):
                j = src.find("\n", i)
                i = n if j < 0 else j
            elif src.startswith("/*", i):
                level, i = 1, i + 2
                while i < n and level:
                    if src.startswith("/*", i):
                        level, i = level + 1, i + 2
                    elif src.startswith("*/", i):
                        level, i = level - 1, i + 2
                    else:
                        i += 1
            elif c == '"' or (c == "#" and _raw_open(i)):
                i = string(i)
            elif c == "(":
                depth, i = depth + 1, i + 1
            elif c == ")":
                if in_interp and depth == 0:
                    return i + 1
                depth, i = depth - 1, i + 1
            else:
                i += 1
        return i

    def _raw_open(i: int) -> bool:
        j = i
        while j < n and src[j] == "#":
            j += 1
        return j < n and src[j] == '"'

    def string(i: int) -> int:
        start = i
        hashes = 0
        while src[i] == "#":
            hashes, i = hashes + 1, i + 1
        quotes = 3 if src.startswith('"""', i) else 1
        i += quotes
        close = '"' * quotes + "#" * hashes
        esc = "\\" + "#" * hashes
        text: list[str] = []
        while i < n and not src.startswith(close, i):
            if src.startswith(esc + "(", i):
                i = code(i + len(esc) + 1, True)
            elif src.startswith(esc, i):
                text.append(src[i + len(esc) : i + len(esc) + 1])
                i += len(esc) + 1
            else:
                text.append(src[i])
                i += 1
        if _CJK.search("".join(text)):
            found.append(line(start))
        return i + len(close)

    code(0, False)
    return found


def _swift_files() -> list[str]:
    root = repo_root()
    return sorted(
        p.relative_to(root).as_posix() for d in _ROOTS for p in (root / d).rglob("*.swift")
    )


def _in_table(rel: str) -> bool:
    p = Path(rel)
    return p.parent.as_posix() == _TABLE_DIR and (
        p.name == "Words.swift" or p.name.startswith("Words+")
    )


def _holders() -> dict[str, list[int]]:
    root = repo_root()
    held = {}
    for rel in _swift_files():
        if _in_table(rel):
            continue
        lines = swift_cjk_literal_lines((root / rel).read_text(encoding="utf-8"))
        if lines:
            held[rel] = lines
    return held


def test_scanner_reads_swift_literals() -> None:
    src = (
        '// 注释\nlet a = "x"\n/* 块 /* 嵌套 */ 注释 */\nlet b = "a \\(f("中")) b"\n'
        'let c = """\n  多行\n"""\nlet d = #"\\n 字"#\nlet e = "\\(x) // not 字 a comment"\n'
        'let f = "ok" // 字\n'
    )
    assert swift_cjk_literal_lines(src) == [4, 5, 8, 9]


def test_cjk_swift_literals_only_in_words_table() -> None:
    """Every Swift file with a Chinese literal is the table, a matcher or on the shrinking list."""
    held = _holders()
    allowed = set(_LEGACY) | set(_MATCHERS) | set(_SAMPLES)
    stray = [f"{rel}:{lines[0]}" for rel, lines in held.items() if rel not in allowed]
    assert not stray, (
        "Chinese string literal outside the phone's words table (ADR 0325). Put the words in"
        " ios/Jarvis/App/Words+<area>.swift with zh and en, or, if the file reads his own words,"
        f" add it to _MATCHERS with a reason ({len(stray)}):\n" + "\n".join(stray)
    )
    gone = [rel for rel in sorted(allowed) if rel not in held]
    assert not gone, (
        "These files hold no Chinese literal any more: delete them from _LEGACY/_MATCHERS in"
        f" {Path(__file__).name} so the list shrinks ({len(gone)}):\n" + "\n".join(gone)
    )
    assert _LEGACY == sorted(set(_LEGACY)), "_LEGACY must be sorted, one path per file"
