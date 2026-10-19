"""Canary — Chinese text in ``jarvis/`` lives only in the language table or an input matcher.

Jarvis speaks the user's language (``language`` in settings.yaml). Fixed text
the user sees or hears is looked up in :mod:`jarvis.shared.lang`, text for
models is English, and only what reads the user's own words (regexes, word
lists, punctuation sets) keeps Chinese beside English. A new Chinese string
anywhere else is a sentence an English user would get in Chinese, so this
enumerates every string literal holding a CJK character (docstrings and
comments excluded) and names each one outside the allowlist below.

Canary house style: stdlib ``ast`` only, no import of the modules under test.
"""

from __future__ import annotations

import ast
import re
from typing import Final

from tests.canary._helpers import iter_jarvis_py_files, parse, repo_root

_CJK: Final = re.compile(r"[　-〿一-鿿＀-￯]")

_TABLE: Final = "jarvis/shared/lang.py"

# Input matchers: they read what the user said or what a model wrote, so the
# Chinese forms stay next to the English ones. Keyed by file, then by the
# enclosing module-level name or function.
_MATCHERS: Final[dict[str, frozenset[str]]] = {
    "jarvis/cli/__init__.py": frozenset({"_LONG_RUN_RE"}),
    "jarvis/decision/__init__.py": frozenset(
        {"_WRITTEN_MARKUP_RE", "_REPEAT_REQUEST_RE", "_repeat_of_last_answer", "_PIECE_END",
         "_FULL_WIDTH_END", "_CLOSERS", "_CLAUSE_END"}
    ),
    # A model-written line before a tool call that already claims a result is not spoken.
    "jarvis/decision/commentary.py": frozenset({"_RESULT_CLAIM"}),
    # The brief reads a saved report's own brackets, citation tails and sentence ends.
    "jarvis/decision/daily_report.py": frozenset(
        {"_COMPLETION_WORDS", "_TITLE_TERM", "_BREAKS", "_REFS_TAIL", "_GROUP", "_SENTENCE_END",
         "_clip", "_status_of"}
    ),
    "jarvis/decision/pre_route.py": frozenset({"_DEMONSTRATIVE_TASK_RE"}),
    # A title the model wrote is refused when it is only where he was: it is read after the "at"
    # or "在" and the stops of either language that wrap it (ADR 0302).
    "jarvis/decision/doing_run.py": frozenset({"_LEADING", "_PUNCTUATION"}),
    # Jev's dismiss option names Chinese send-offs as examples of what Allen says.
    "jarvis/decision/voice_words.py": frozenset({"_CRITERIA"}),
    # A coding agent's final paragraph asks Allen when it holds a full-width question mark too.
    "jarvis/decision/turn_end_asks.py": frozenset({"_asks_text"}),
    # An answer that says it saved, will look something up or looked it up is read by its words
    # (ADR 0240, ADR 0250).
    "jarvis/decision/turn_guards.py": frozenset(
        {"_DID", "_WILL", "_SENTENCE_END", "_QUESTION", "_SCREEN", "_STORE_ZH", "_CALENDAR_ZH",
         "_WHEN_ZH", "_ACT", "_CONTENT", "_CITES", "_RIDE_TOPIC", "_SET_CLAIM"},
    ),
    "jarvis/decision/stream_gate.py": frozenset({"routine_stream_policy"}),
    "jarvis/decision/stream_risk.py": frozenset(
        {"_HIGH", "_CONSEQUENTIAL", "_AMBIGUOUS", "_PERSONAL_OR_IMPERATIVE",
         "_EXPLANATORY_FORM", "_GREETINGS", "_supported_candidate", "_text_risk"}
    ),
    "jarvis/decision/stream_sentences.py": frozenset(
        {"_SENTENCE_ENDS", "_CLAUSE_ENDS", "_CLOSERS", "_balanced_prose"}
    ),
    # A shut Dashboard opens only when his words ask to see something.
    "jarvis/execution/dashboard_tool.py": frozenset({"_ASKS_TO_SEE"}),
    # The browser guard (ADR 0059) reads a page's own labels, Chinese sites included.
    "jarvis/execution/browser_guard.py": frozenset({"_MONEY", "_CARD", "_SECRET"}),
    "jarvis/execution/path_resolver.py": frozenset({"_TRAILING_NOUN_RE", "_CONNECTIVE_CHAR"}),
    # The ask card (ADR 0066) refuses fields whose model-written labels ask for a secret.
    "jarvis/execution/tools.py": frozenset({"_ASK_SECRET_RE"}),
    # `save_place`'s description (ADR 0234) names a Chinese sentence as an example of what Allen
    # says when he names a place.
    "jarvis/execution/place_tools.py": frozenset({"SAVE_DESCRIPTION"}),
    # `keep`'s description (ADR 0245) names the Chinese phrases Allen says to have something kept.
    "jarvis/execution/keep_tool.py": frozenset({"_KEEP_DESCRIPTION"}),
    # Typlus's polish prompt (ADR 0058) names the Chinese phantoms the recognizer emits
    # and Chinese dictations that must not be answered, as the words the model will read.
    # Listening sounds stripped at a repeated sentence's edges (ADR 0268).
    "jarvis/runtime/device_voice.py": frozenset({"_FILLERS"}),
    "jarvis/runtime/dictation.py": frozenset({"POLISH_PROMPT", "COMMAND_PROMPT"}),
    # A question naming a past day is the saved report's to answer; these read the question.
    # Verification-code mail is read by the words Chinese services write around the code (ADR 0266).
    "jarvis/runtime/mail_codes.py": frozenset({"_STRONG", "_NOT_LOGIN"}),
    # A letter is read for a sign of a date or an hour in Chinese or English (ADR 0274).
    "jarvis/decision/commitment_mail.py": frozenset({"_SIGNS"}),
    "jarvis/runtime/daily_report.py": frozenset({"_PAST_DAY", "_YESTERDAY"}),
    # A deep-search brief written in Chinese opens its list of pages with a 来源 line.
    "jarvis/runtime/deep_search.py": frozenset({"_SOURCES_LINE"}),
    "jarvis/execution/tool_search.py": frozenset({"_TOKEN"}),
    "jarvis/shared/text.py": frozenset({"is_english"}),
    # The six core-memory section names (ADR 0146) are stored keys and the exact words the
    # consolidation prompt and the `remember` enum name; they are not translated.
    "jarvis/state/core_memory.py": frozenset(
        {"SECTION_CAPS", "DEFAULT_SECTION", "DUE_SECTION", "ID_FREE_SECTION", "GOALS_SECTION",
         "_LEGACY"},
    ),
    "jarvis/state/daily_report.py": frozenset({"MILESTONES", "_QUESTION"}),
    # The WeChat Pay and Alipay statements (ADR 0259) name their columns and statuses in Chinese.
    # The Claude daily letter's subject (ADR 0295) is written in Chinese by his Claude task.
    "jarvis/state/import_parsers.py": frozenset(
        {"DAILY_SUBJECT", "SUCCESS", "_DIRECTIONS", "_fen", "_payment", "wechat_pay", "alipay",
         "_BOC_PAGE_WORDS", "_BOC_CURRENCY", "_boc_cells"}
    ),
    "jarvis/state/work_state.py": frozenset({"_STOP_WORDS", "_TERM", "_CJK_RUN"}),
    # A call is read from the front window; Tencent Meeting titles its lobby 腾讯会议 (ADR 0161).
    "jarvis/state/timesink_moment.py": frozenset({"call_app"}),
    "jarvis/surface/sentence_splitter.py": frozenset({"_DELIMITERS"}),
    "jarvis/surface/voice_asr.py": frozenset(
        {"_MISPLACED_PERIOD", "_ACTION_WORDS", "_SIMPLIFIED_PROMPT",
         "_TERMINAL_PUNCTUATION", "_DANGLING_SUFFIXES", "_is_punctuation_only", "_WAKE_ONLY_RE",
         "_WAKE_LEAD_RE", "_BACKCHANNEL_UNIT", "_STOP_PHRASE",
         "_STOP_REQUEST_RE", "_DISMISS_RE", "_WAIT_UNIT",
         "_DISMISS_WORD_RE", "_QUESTION_END_RE", "_QUIET_RES", "_QUIET_WRAP",
         "_SHORT_ANSWER_RE",
         "caption_text", "is_backchannel", "is_unclear_sound", "_WHISPER_SILENCE_RE",
         "WhisperFinalRecognizer.recognize", "HybridFinalRecognizer._hear",
         "HybridFinalRecognizer._hear_once", "_short_unclear", "_FULL_WIDTH"}
    ),
    # The silence trimmer (ADR 0165) reads the punctuation that ended a segment's text.
    "jarvis/surface/tts_silence.py": frozenset({"_CLAUSE_ENDS", "_CLOSERS"}),
    "jarvis/surface/voice_cues.py": frozenset({"_LAUGH_WORDS"}),
    "jarvis/surface/voice_live.py": frozenset({"_SENTENCE_ENDS"}),
}


def _cjk_literals(tree: ast.Module) -> list[tuple[str, int]]:
    """(enclosing name, line) for every non-docstring string constant holding CJK."""
    found: list[tuple[str, int]] = []

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{scope}.{child.name}" if scope else child.name
            elif not scope and isinstance(child, (ast.Assign, ast.AnnAssign)):
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                name = next((t.id for t in targets if isinstance(t, ast.Name)), "")
            if isinstance(child, ast.Expr) and isinstance(child.value, ast.Constant):
                continue  # docstring or bare string statement
            if (
                isinstance(child, ast.Constant)
                and isinstance(child.value, str)
                and _CJK.search(child.value)
            ):
                found.append((name, child.lineno))
            visit(child, name)

    visit(tree, "")
    return found


def test_cjk_string_literals_only_in_table_or_matchers() -> None:
    """Every CJK string literal in jarvis/ is in the language table or an input matcher."""
    root = repo_root()
    stray: list[str] = []
    for path in iter_jarvis_py_files():
        rel = path.relative_to(root).as_posix()
        if rel == _TABLE:
            continue
        allowed = _MATCHERS.get(rel, frozenset())
        for name, line in _cjk_literals(parse(path)):
            if name not in allowed:
                stray.append(f"{rel}:{line} ({name or '<module>'})")
    assert not stray, (
        "Chinese outside jarvis/shared/lang.py and the input matchers — put user text in the"
        f" table and model text in English ({len(stray)}):\n" + "\n".join(stray)
    )
