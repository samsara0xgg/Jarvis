"""No test file imports from another test file.

A helper that two tests share lives in ``tests/support/``, under a public name,
and both import it from there. A test file that is also a library makes every
rename in it a hunt through its importers, and it runs its own tests again in
each importer's collection when it re-exports a fixture.

The check reads every ``.py`` under ``tests/`` and flags an import whose module
is a ``test_*`` file: ``from tests.integration.test_x import y``,
``from tests.integration import test_x``, ``import tests.integration.test_x``
and the relative forms. ``tests/canary/_helpers.py`` and the shared fakes in
``tests/integration/`` that are not test files (``weatherkit_world.py``, the
``mcp_*_server.py`` stand-ins) are not test files and stay importable.

``KNOWN`` is the migration backlog: each entry is a pair a later pass removes
once it moves the helper out. An entry that no longer matches an import fails
the check too, so the list can only shrink.
"""

from __future__ import annotations

import ast

from tests.canary._helpers import parse, relative_to_repo, repo_root

# The migration backlog: (importing file, imported test file) pairs, by file name, that still
# import across test files because the helper lives in a file another pass is rewriting
# (job_mail, soft_barge_in, timesink_activity, worker_confirmation, anthropic_adaptation,
# terminal_listen). That pass moves each helper to ``tests/support/`` and deletes its pairs
# here.
KNOWN: frozenset[tuple[str, str]] = frozenset(
    {
        # The files being rewritten, which still take their helpers from the files they import.
        ("test_job_mail", "test_audio_output"),
        ("test_job_mail", "test_lifecycle_commentary"),
        ("test_soft_barge_in", "test_voice_short_sounds"),
        ("test_terminal_listen", "test_terminal_voice"),
        # Files that take a helper from one of those: the helper stays where it is until its
        # file is rewritten, so they cannot import it from tests/support yet.
        ("test_conversation_model", "test_anthropic_adaptation"),
        ("test_picture_history", "test_anthropic_adaptation"),
        ("test_turn_timing", "test_anthropic_adaptation"),
        ("test_device_voice", "test_soft_barge_in"),
        ("test_exit_stops_speech", "test_soft_barge_in"),
        ("test_jev_oneshot", "test_soft_barge_in"),
        ("test_utterance_judged_on_receipt", "test_soft_barge_in"),
        ("test_voice_cues", "test_soft_barge_in"),
        ("test_voice_words_jev", "test_soft_barge_in"),
        ("test_voiceprint_gate", "test_soft_barge_in"),
        ("test_job_ledger_tool", "test_job_mail"),
        ("test_terminal_device_views", "test_job_mail"),
        ("test_timesink_moment", "test_job_mail"),
        ("test_terminal_elsewhere", "test_terminal_listen"),
        ("test_terminal_ui", "test_terminal_listen"),
        ("test_utterance_judged_on_receipt", "test_terminal_listen"),
        ("test_voiceprint_gate", "test_terminal_listen"),
        ("test_daily_report", "test_timesink_activity"),
        ("test_projects", "test_timesink_activity"),
        ("test_terminal_reads", "test_timesink_activity"),
        ("test_work_state", "test_timesink_activity"),
        # Moves that wait on the same files: these helpers import from a file listed above.
        ("test_terminal_device_views", "test_projects"),
        ("test_terminal_device_views", "test_timesink_moment"),
        ("test_work_state_live", "test_work_state"),
    }
)


def _is_test_module(name: str) -> bool:
    return name.rsplit(".", 1)[-1].startswith("test_")


def _imported_test_modules(tree: ast.Module, importer_module: str) -> set[str]:
    """The ``test_*`` modules ``tree`` imports, as dotted names."""
    package = importer_module.rsplit(".", 1)[0]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names if _is_test_module(a.name))
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")
                parts = parts[: len(parts) - node.level + 1]
                base = ".".join([*parts, *([base] if base else [])])
            if _is_test_module(base):
                found.add(base)
            found.update(f"{base}.{a.name}" for a in node.names if _is_test_module(a.name))
    return found


def _violations() -> set[tuple[str, str]]:
    """(importing file, imported test file), each by file name without ``.py``."""
    found: set[tuple[str, str]] = set()
    for path in sorted((repo_root() / "tests").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        module = relative_to_repo(path).removesuffix(".py").replace("/", ".")
        found.update(
            (path.stem, target.rsplit(".", 1)[-1])
            for target in _imported_test_modules(parse(path), module)
        )
    return found


def test_no_test_file_imports_another_test_file() -> None:
    """Shared helpers come from ``tests/support/``, never from a ``test_*`` file."""
    found = _violations()
    new = sorted(found - KNOWN)
    assert not new, (
        "move the helper to tests/support/ and import it from there:\n"
        + "\n".join(f"  {importer} imports {module}" for importer, module in new)
    )
    stale = sorted(KNOWN - found)
    assert not stale, (
        "these known exceptions no longer import a test file; delete them from KNOWN:\n"
        + "\n".join(f"  {importer} imports {module}" for importer, module in stale)
    )
