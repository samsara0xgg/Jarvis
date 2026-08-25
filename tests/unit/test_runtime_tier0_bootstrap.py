"""Unit tests for the Tier 0 whitelist load at the composition root (spec §17).

``bootstrap_runtime_app`` is the only place that reads
``config/tier0_patterns.yaml``: it sits next to ``jarvis.yaml`` so Allen
edits one config directory, and both surfaces that boot through the
composition root (the daemon and the one-shot CLI) inherit Tier 0 from
this single load.

The three boot behaviors pinned here are the ones a hand-edited pattern
file can hit:

- a well-formed file lands on ``JarvisRuntime.tier0_table`` in file
  order (order is load-bearing — ``match_tier0`` is first-match-wins);
- an absent file is the Day-1 state, not an error: Tier 0 is simply
  disabled and every utterance falls through to the LLM;
- a file naming a tool the ``regex_router`` principal may not dispatch
  fails the boot loudly as :class:`RuntimeBootstrapError`, rather than
  silently dropping the entry and leaving Allen wondering why his
  pattern never fires.

The last test pins the *shipped* whitelist itself: the two Day-1
patterns must survive a real boot, actually match the utterances they
were written for, and stay clear of completion-class phrasing (the
Pre-emit Gate would downgrade a Tier 0 response that claimed
completion).

All tests are Tier 1 (LLM-free) — bootstrap constructs the LLM client
lazily and never calls ``chat``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from jarvis.decision.pre_emit_phrases import COMPLETION_REGEXES
from jarvis.decision.tier0 import match_tier0
from jarvis.runtime import RuntimeBootstrapError, bootstrap_runtime_app

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_PATH = _REPO_ROOT / "config" / "jarvis.yaml"
_PROMPT_PATH = _REPO_ROOT / "prompts" / "jarvis_v1.md"

# Two structurally valid entries naming a tool ``regex_router`` really
# may call. The regexes are deliberately ASCII toys — the shipped
# Chinese patterns are exercised by the last test against the real file.
_VALID_TWO_PATTERNS = (
    "- id: time_now\n"
    '  pattern: "^what time$"\n'
    "  tool: get_current_time\n"
    '  template: "it is {spoken_time}"\n'
    "- id: date_today\n"
    '  pattern: "^what date$"\n'
    "  tool: get_current_time\n"
    '  template: "it is {spoken_date}"\n'
)


def _make_config_dir(tmp_path: Path, *, tier0_yaml: str | None) -> Path:
    """Build a self-contained config tree under ``tmp_path``; return its ``config/``.

    ``bootstrap_runtime_app`` derives the repo root from
    ``config_path.parent.parent``, so the prompt must sit beside the
    copied config as ``prompts/jarvis_v1.md`` or the boot aborts before
    it ever reaches the Tier 0 load.

    Args:
        tmp_path: pytest tmp dir that becomes the synthetic repo root.
        tier0_yaml: Raw ``tier0_patterns.yaml`` text, or ``None`` to
            leave the file absent (Tier 0 disabled).

    Returns:
        Path to the created ``config/`` directory.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    shutil.copyfile(_CONFIG_PATH, config_dir / "jarvis.yaml")
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    shutil.copyfile(_PROMPT_PATH, prompt_dir / "jarvis_v1.md")
    if tier0_yaml is not None:
        (config_dir / "tier0_patterns.yaml").write_text(tier0_yaml, encoding="utf-8")
    return config_dir


def test_bootstrap_loads_tier0_table(tmp_path: Path) -> None:
    """A valid pattern file lands on ``JarvisRuntime.tier0_table`` in file order."""
    cfg = _make_config_dir(tmp_path, tier0_yaml=_VALID_TWO_PATTERNS)
    rt = bootstrap_runtime_app(config_path=cfg / "jarvis.yaml", runtime_root=tmp_path / "rt")
    try:
        assert [p.pattern_id for p in rt.tier0_table] == ["time_now", "date_today"]
    finally:
        rt.conn.close()


def test_bootstrap_missing_tier0_file_disables_tier0(tmp_path: Path) -> None:
    """No ``tier0_patterns.yaml`` is a supported state: Tier 0 off, boot fine."""
    cfg = _make_config_dir(tmp_path, tier0_yaml=None)
    rt = bootstrap_runtime_app(config_path=cfg / "jarvis.yaml", runtime_root=tmp_path / "rt")
    try:
        assert rt.tier0_table == ()
    finally:
        rt.conn.close()


def test_bootstrap_rejects_invalid_tier0_tool(tmp_path: Path) -> None:
    """A pattern targeting a tool ``regex_router`` may not call fails the boot."""
    bad = '- id: bad\n  pattern: "^x$"\n  tool: spawn_worker\n  template: "t"\n'
    cfg = _make_config_dir(tmp_path, tier0_yaml=bad)
    with pytest.raises(RuntimeBootstrapError, match="spawn_worker"):
        bootstrap_runtime_app(config_path=cfg / "jarvis.yaml", runtime_root=tmp_path / "rt")


def test_shipped_tier0_patterns_match_and_avoid_completion_language(tmp_path: Path) -> None:
    """The shipped whitelist boots, matches its target utterances, and stays scrub-safe.

    A typo in a shipped regex is invisible at boot (the file still
    validates) and shows up only as Tier 0 never firing, so the match
    check is the real guard here. The completion-keyword check pins the
    other shipping constraint: a Tier 0 template that claimed completion
    would be downgraded by the Pre-emit Gate.
    """
    rt = bootstrap_runtime_app(
        config_path=_CONFIG_PATH,
        prompt_path=_PROMPT_PATH,
        runtime_root=tmp_path,
    )
    try:
        table = rt.tier0_table
        assert [p.pattern_id for p in table] == ["time_now", "date_today"]

        time_hit = match_tier0("现在几点了", table)
        assert time_hit is not None
        assert time_hit.pattern_id == "time_now"
        assert time_hit.tool_name == "get_current_time"

        date_hit = match_tier0("今天几号", table)
        assert date_hit is not None
        assert date_hit.pattern_id == "date_today"

        for pattern in table:
            for regex in COMPLETION_REGEXES:
                assert regex.search(pattern.response_template) is None, (
                    f"shipped tier0 pattern {pattern.pattern_id!r} template uses "
                    f"completion-class phrasing matching {regex.pattern!r}; the "
                    f"Pre-emit Gate would downgrade the response."
                )
    finally:
        rt.conn.close()
