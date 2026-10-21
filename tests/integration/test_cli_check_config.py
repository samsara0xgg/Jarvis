"""``serve --check-config`` and ``terminal --check-config``: the config a start would load.

A deploy restarts the brain and the Mac terminal over a runtime root whose ``settings.yaml`` and
Settings store sit on top of the shipped config, and the loader refuses a malformed value by
design. The flag loads the config through the same function the entry point calls and says whether
it is accepted, so the deploy can refuse before it restarts. It must leave the root exactly as it
found it, start nothing, and read no key.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from typing import TYPE_CHECKING

import pytest

from jarvis.cli import main
from jarvis.deployment.process_lock import acquire_exclusive
from jarvis.shared.lang import Language, t
from tests.support.runtime_config import SHIPPED

if TYPE_CHECKING:
    from pathlib import Path

ENTRIES = {
    "serve": ["serve"],
    "terminal": ["terminal", "--brain", "http://jarvis:8006"],
}
SECRET = "sk-check-config-must-not-load-this"  # noqa: S105 - a fixture, not a credential


def _snapshot(root: Path) -> dict[str, tuple[int, int, int, bytes | None]]:
    """Every path under ``root``: mode, size, modification time and a file's bytes."""
    return {
        str(one.relative_to(root)): (
            one.stat().st_mode, one.stat().st_size, one.stat().st_mtime_ns,
            one.read_bytes() if one.is_file() else None,
        )
        for one in [root, *root.rglob("*")]
    }


def _root(tmp_path: Path, settings: str, store: str = '{"reply_language": "en"}') -> Path:
    """A root as the Pi's is: settings, the Settings page's store, a key, a database, no token."""
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    (root / "settings.yaml").write_text(settings, encoding="utf-8")
    (root / "settings.json").write_text(store, encoding="utf-8")
    (root / "env").write_text(f"ANTHROPIC_API_KEY={SECRET}\n", encoding="utf-8")
    (root / "memory.db").write_bytes(b"conversations")
    return root


@pytest.fixture(autouse=True)
def _no_key_in_the_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("JARVIS_RUNTIME_ROOT", raising=False)


@pytest.mark.parametrize("entry", ENTRIES)
def test_an_accepted_config_prints_one_line_and_leaves_the_root_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], entry: str,
) -> None:
    """Exit 0, one line on stdout, no file changed, no thread, no key loaded, no token read."""
    root = _root(tmp_path, "work_state:\n  auto: true\nlocate:\n  max_age_s: 600\n")
    before, threads = _snapshot(root), set(threading.enumerate())

    assert main([*ENTRIES[entry], "--check-config", "--runtime-root", str(root)]) == 0

    out, err = capsys.readouterr()
    if entry == "serve":
        line = t("cli.config_ok", lang="zh", config=SHIPPED, root=root.resolve(), role="all")
    else:
        line = t("cli.terminal_config_ok", lang="zh", config=SHIPPED, root=root.resolve())
    assert out == f"jarvis {entry}: {line}\n"
    assert not err
    assert _snapshot(root) == before
    assert not set(threading.enumerate()) - threads  # none started; one a past test left may end
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert SECRET not in out + err


@pytest.mark.parametrize("entry", ENTRIES)
def test_a_root_that_does_not_exist_is_not_made_by_the_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], entry: str,
) -> None:
    """A first start's root is laid out by the start, not by the check."""
    root = tmp_path / "not-yet"

    assert main([*ENTRIES[entry], "--check-config", "--runtime-root", str(root)]) == 0

    assert capsys.readouterr().out.count("\n") == 1
    assert not root.exists()


@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize(
    ("settings", "named"),
    [
        ("locate:\n  max_age_s: 0\n", "locate.max_age_s"),
        ("work_state:\n  auto: yes please\n", "work_state.auto"),
        ("runtime:\n  role: sideways\n", "runtime.role"),
        ("work_state:\n  timezone: Mars/Olympus\n", "work_state.timezone"),
        ("locate: [\n", "is not valid YAML"),
        ("observer:\n  timesink:\n    db_path: ''\n", "observer.timesink.db_path"),
    ],
)
def test_a_refused_config_prints_the_refusal_on_stderr_and_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], entry: str, settings: str, named: str,
) -> None:
    """The loader's own text, on one line, stdout empty, and the root as it was.

    The last row is accepted by the file alone and refused once the Settings page's store
    (``timesink`` on) lies over it: the check loads the store too.
    """
    root = _root(tmp_path, settings, store='{"timesink": true}')
    before, threads = _snapshot(root), set(threading.enumerate())

    assert main([*ENTRIES[entry], "--check-config", "--runtime-root", str(root)]) == 1

    out, err = capsys.readouterr()
    assert not out
    assert err.count("\n") == 1
    assert err.startswith(f"jarvis {entry}: ")
    assert t("cli.config_refused", lang="zh", reason="")[:5] in err
    assert named in err
    assert _snapshot(root) == before
    assert not set(threading.enumerate()) - threads  # none started; one a past test left may end
    assert SECRET not in err


@pytest.mark.parametrize("language", ["zh", "en"])
def test_the_accepted_line_is_in_the_configs_language(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], language: Language,
) -> None:
    """Fixed text goes through lang: the root's ``language`` picks the wording."""
    root = _root(tmp_path, f"language: {language}\nruntime:\n  role: brain\n")

    assert main(["serve", "--check-config", "--runtime-root", str(root)]) == 0

    line = t("cli.config_ok", lang=language, config=SHIPPED, root=root.resolve(), role="brain")
    assert capsys.readouterr().out == f"jarvis serve: {line}\n"


@pytest.mark.parametrize("language", ["zh", "en"])
def test_the_terminals_accepted_line_names_no_role(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], language: Language,
) -> None:
    """The terminal runs the same whatever ``runtime.role`` says, so its line does not claim one."""
    root = _root(tmp_path, f"language: {language}\nruntime:\n  role: brain\n")

    assert main(["terminal", "--check-config", "--runtime-root", str(root)]) == 0

    line = t("cli.terminal_config_ok", lang=language, config=SHIPPED, root=root.resolve())
    assert capsys.readouterr().out == f"jarvis terminal: {line}\n"


def test_the_terminal_check_validates_the_brain_address_but_reads_no_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """``--brain`` is checked as a start checks it; the token file is not looked for."""
    root = _root(tmp_path, "")
    assert not (root / "brain-token").exists()

    bad = ["terminal", "--check-config", "--brain", "ftp://nope", "--runtime-root", str(root)]
    assert main(bad) == 1
    assert "jarvis terminal: " in capsys.readouterr().err
    assert main(["terminal", "--check-config", "--runtime-root", str(root)]) == 0
    assert not (root / "brain-token").exists()


def test_a_terminal_without_the_flag_still_needs_a_brain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Only the check may leave ``--brain`` out."""
    with pytest.raises(SystemExit) as exit_info:
        main(["terminal", "--runtime-root", str(tmp_path)])
    assert exit_info.value.code == 2
    assert "--brain" in capsys.readouterr().err


def _serve_check(root: Path, home: Path) -> subprocess.CompletedProcess[str]:
    """``python -m jarvis serve --check-config`` as the deploy runs it."""
    return subprocess.run(  # noqa: S603 - sys.executable is trusted; argv is fixed.
        [sys.executable, "-m", "jarvis", "serve", "--check-config", "--runtime-root", str(root)],
        capture_output=True, text=True, timeout=60, check=False,
        env={**os.environ, "HOME": str(home)},
    )


def test_the_check_runs_beside_a_daemon_that_holds_the_root(tmp_path: Path) -> None:
    """A deploy checks while the old brain is up: the lock is neither taken nor waited for."""
    root = _root(tmp_path, "locate:\n  max_age_s: 600\n")
    with acquire_exclusive(root / "daemon.lock"):
        before = _snapshot(root)
        good = _serve_check(root, tmp_path)
        assert _snapshot(root) == before
        (root / "settings.yaml").write_text("locate:\n  max_age_s: 0\n", encoding="utf-8")
        refused = _serve_check(root, tmp_path)

    assert (good.returncode, good.stdout.count("\n"), good.stderr) == (0, 1, "")
    assert (refused.returncode, refused.stdout, refused.stderr.count("\n")) == (1, "", 1)
    assert "locate.max_age_s" in refused.stderr
