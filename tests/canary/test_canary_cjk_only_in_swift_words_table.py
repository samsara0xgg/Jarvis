"""Canary — Chinese in the iPhone app's Swift string literals lives only in the words table.

The phone words every fixed string from ``ios/Jarvis/App/Words.swift`` and its
``Words+<area>.swift`` siblings, in the language the brain's settings name (ADR 0327). Any other
Swift file holding a Chinese literal says why where it is defined, on a line of its own
(ADR 0325: a canary's rule sits with the thing, not in a list of file names):

- ``// words: legacy`` — screen words from before the table; they move into it file by file, and
  ``_LEGACY_FILES`` counts the files still marked, so the count only goes down.
- ``// words: his — <reason>`` — code that reads his own words (word lists for matching him).
- ``// words: sample — <reason>`` — DEBUG made-up data standing in for what the brain sends.

A mark on a file that holds no Chinese literal any more fails too, so marks do not outlive their reason.

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

_MARK: Final = re.compile(r"^// words: (legacy|his|sample)\b", re.M)

# Files still marked ``// words: legacy``. Lower it in the commit that moves one into the table;
# it never goes up, since new code words through the table.
_LEGACY_FILES: Final = 87


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


def _marks() -> dict[str, str]:
    root = repo_root()
    found = {}
    for rel in _swift_files():
        m = _MARK.search((root / rel).read_text(encoding="utf-8"))
        if m:
            found[rel] = m.group(1)
    return found


def test_cjk_swift_literals_only_in_words_table() -> None:
    """Every Swift file with a Chinese literal is the table or says why; marks match what files hold."""
    held, marks = _holders(), _marks()
    stray = [f"{rel}:{lines[0]}" for rel, lines in held.items() if rel not in marks]
    assert not stray, (
        "Chinese string literal outside the phone's words table (ADR 0327). Put the words in"
        " ios/Jarvis/App/Words+<area>.swift with zh and en; if the file reads his own words or"
        f" holds DEBUG made-up data, mark it `// words: his|sample — <reason>` ({len(stray)}):\n"
        + "\n".join(stray)
    )
    stale = [rel for rel in sorted(marks) if rel not in held]
    assert not stale, (
        "These files are marked `// words:` but hold no Chinese literal any more: delete the mark"
        f" ({len(stale)}):\n" + "\n".join(stale)
    )
    legacy = sum(kind == "legacy" for kind in marks.values())
    assert legacy <= _LEGACY_FILES, (
        f"{legacy - _LEGACY_FILES} more file(s) marked `// words: legacy` than before; new screen"
        " words go in the table (ADR 0327)."
    )
    assert legacy == _LEGACY_FILES, (
        f"{_LEGACY_FILES - legacy} file(s) left the legacy mark: lower _LEGACY_FILES to {legacy}"
        f" in {Path(__file__).name}."
    )
