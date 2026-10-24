"""``scripts/deploy-brain.sh`` checks the brain's config before it restarts the brain (ADR 0370).

The script runs on the Pi, in ``~/jarvis-brain``, against its bare origin. Here ``HOME`` is a
temporary directory holding a real git checkout of a real bare repository, and the venv's
interpreter, ``systemctl``, ``curl``, ``sleep`` and ``uv`` are small scripts on ``PATH`` that log
each call to one file, so the order of what ran is the file's order. What is asserted is what the
owner relies on: a config the check refuses leaves the checkout, and the venv's lock, where they
were and restarts nothing; a config it accepts restarts after the check; ``--dry-run`` checks the
new tree from a temporary export and moves nothing.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "deploy-brain.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("flock") is None or shutil.which("git") is None,
    reason="the script needs flock and git",
)

REFUSAL = "jarvis serve: Config refused: locate: locate.max_age_s is a whole number of seconds"

FAKE_PYTHON = """#!/bin/bash
echo "check cwd=$PWD root=$JARVIS_RUNTIME_ROOT tmp=$SQLITE_TMPDIR args=$* \\
config=$([ -f config/jarvis.yaml ] && echo yes || echo no) \\
code=$([ -f jarvis/__init__.py ] && echo yes || echo no)" >> "$HOME/calls.log"
if [ -e "$HOME/refuse" ]; then
    echo "WARNING a warning that comes before the refusal" >&2
    echo "jarvis serve: Config refused: locate: locate.max_age_s is a whole number of seconds" >&2
    exit 1
fi
echo "jarvis serve: Config is good: x"
"""


FAKES = {
    "systemctl": (
        '#!/bin/bash\necho "systemctl $*" >> "$HOME/calls.log"\n'
        '[ "$1 $2" = "--user show" ] && echo -1000000000000\nexit 0\n'
    ),
    "curl": '#!/bin/bash\necho "curl" >> "$HOME/calls.log"\necho 200\n',
    "sleep": '#!/bin/bash\necho "sleep $*" >> "$HOME/calls.log"\n',
}


def _git(env: dict[str, str], where: Path, *args: str) -> str:
    """One git command in ``where``; its stripped stdout."""
    done = subprocess.run(  # noqa: S603 - git, a fixed argv.
        ["git", "-C", str(where), *args],  # noqa: S607
        capture_output=True, text=True, check=True, env=env,
    )
    return done.stdout.strip()


def _commit(env: dict[str, str], where: Path, message: str) -> None:
    _git(env, where, "add", "-A")
    _git(env, where, "commit", "-q", "-m", message)


@dataclass
class Pi:
    """The Pi: its home, its checkout, the seed clone that feeds origin, and what to run with."""

    home: Path
    repo: Path
    seed: Path
    old: str
    new: str
    env: dict[str, str]

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        """The script, as the owner runs it."""
        return subprocess.run(  # noqa: S603 - the repo's own script, a fixed argv.
            ["/bin/bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=120,
            check=False, env=self.env, cwd=self.home,
        )

    def calls(self) -> list[str]:
        """Every fake command and check that ran, in order."""
        log = self.home / "calls.log"
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    def head(self) -> str:
        """The checkout's commit."""
        return _git(self.env, self.repo, "rev-parse", "HEAD")

    def refuse(self) -> None:
        """Make the fake interpreter refuse the config, as the real one does."""
        (self.home / "refuse").write_text("", encoding="utf-8")


def _pi(tmp_path: Path, *, lock_moves: bool = False) -> Pi:
    """A Pi at commit ``old`` whose origin has ``new`` (a lock change too, when asked)."""
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    (home / ".jarvis").mkdir(parents=True)
    (home / ".local" / "bin").mkdir(parents=True)
    bin_dir.mkdir()
    for name, text in FAKES.items():
        (bin_dir / name).write_text(text, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    uv = home / ".local" / "bin" / "uv"
    uv.write_text('#!/bin/bash\necho "uv $*" >> "$HOME/calls.log"\n', encoding="utf-8")
    uv.chmod(0o755)
    env = {
        "HOME": str(home), "PATH": f"{bin_dir}:{os.environ['PATH']}", "TMPDIR": str(tmp_path),
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
        # The script's `date` must name the same local day the plan below is written for.
        **({"TZ": os.environ["TZ"]} if "TZ" in os.environ else {}),
    }
    origin, seed, repo = tmp_path / "origin.git", tmp_path / "seed", home / "jarvis-brain"
    _git(env, tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(env, tmp_path, "clone", "-q", str(origin), str(seed))
    (seed / "jarvis").mkdir()
    (seed / "jarvis" / "__init__.py").write_text("", encoding="utf-8")
    (seed / "config").mkdir()
    (seed / "config" / "jarvis.yaml").write_text("language: en\n", encoding="utf-8")
    (seed / "prompts").mkdir()
    (seed / "prompts" / "jarvis_v1.md").write_text("prompt\n", encoding="utf-8")
    (seed / "data").mkdir()
    (seed / "data" / "pricing.json").write_text("{}\n", encoding="utf-8")
    (seed / "uv.lock").write_text("lock one\n", encoding="utf-8")
    _commit(env, seed, "old")
    _git(env, seed, "push", "-q", "origin", "HEAD:main")
    _git(env, tmp_path, "clone", "-q", str(origin), str(repo))
    python = repo / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(FAKE_PYTHON, encoding="utf-8")
    python.chmod(0o755)
    pi = Pi(home, repo, seed, "", "", env)
    pi.old = _git(env, repo, "rev-parse", "--short", "HEAD")
    (seed / "jarvis" / "new_module.py").write_text("x = 1\n", encoding="utf-8")
    if lock_moves:
        (seed / "uv.lock").write_text("lock two\n", encoding="utf-8")
    _commit(env, seed, "new")
    _git(env, seed, "push", "-q", "origin", "HEAD:main")
    pi.new = _git(env, seed, "rev-parse", "--short", "HEAD")
    # The events the script reads: a plan drafted today, and no turn at all.
    with sqlite3.connect(home / ".jarvis" / "mac_events.db") as db:
        db.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY, type TEXT, ts_epoch_ms INTEGER,"
            " payload_json TEXT)",
        )
        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        db.execute(
            "INSERT INTO events (type, ts_epoch_ms, payload_json) VALUES"
            " ('plan.drafted', 0, ?)", (f'{{"day": "{today}"}}',),
        )
    return pi


def _checks(pi: Pi) -> list[str]:
    return [line for line in pi.calls() if line.startswith("check ")]


def test_a_refused_config_puts_the_checkout_back_and_restarts_nothing(tmp_path: Path) -> None:
    """The old brain keeps running on the old code; the one line says why."""
    pi = _pi(tmp_path)
    before = pi.head()
    pi.refuse()

    done = pi.run()

    assert done.returncode == 1
    assert done.stdout == ""
    assert done.stderr == (
        f"deploy: {pi.old} -> {pi.new} refused: config does not parse ({REFUSAL})\n"
    )
    assert pi.head() == before
    assert not (pi.repo / "jarvis" / "new_module.py").exists()
    assert _git(pi.env, pi.repo, "status", "--porcelain", "--untracked-files=no") == ""
    assert all(not line.startswith(("systemctl", "curl", "uv")) for line in pi.calls())
    (check,) = _checks(pi)
    assert f"cwd={pi.repo.resolve()} " in check
    assert f"root={pi.home}/.jarvis tmp=/tmp args=-m jarvis serve --check-config " in check


def test_a_refused_config_after_a_lock_change_syncs_the_venv_back(tmp_path: Path) -> None:
    """The venv follows the checkout: forward for the check, back when it is refused."""
    pi = _pi(tmp_path, lock_moves=True)
    pi.refuse()

    done = pi.run()

    assert done.returncode == 1
    assert (pi.repo / "uv.lock").read_text(encoding="utf-8") == "lock one\n"
    calls = pi.calls()
    assert [line.split()[0] for line in calls] == ["uv", "check", "uv"]
    assert not any(line.startswith("systemctl") for line in calls)


def test_an_accepted_config_restarts_the_brain_after_the_check(tmp_path: Path) -> None:
    """The check comes first, then the restart and the health probe."""
    pi = _pi(tmp_path)

    done = pi.run()

    assert done.returncode == 0, done.stderr
    assert pi.head().startswith(pi.new)
    assert f"deploy: {pi.old} -> {pi.new}, restarted " in done.stdout
    calls = pi.calls()
    restart = calls.index("systemctl --user restart jarvis-brain")
    assert calls.index(_checks(pi)[0]) < restart
    assert len(_checks(pi)) == 1


def test_a_dry_run_checks_the_new_tree_from_an_export_and_moves_nothing(tmp_path: Path) -> None:
    """The venv's interpreter, run from a temporary copy of origin/main that is gone afterwards."""
    pi = _pi(tmp_path)
    before = pi.head()

    done = pi.run("--dry-run")

    assert done.returncode == 0, done.stderr
    assert f"dry-run: {pi.old} -> {pi.new} (1 commits), uv.lock same, config ok," in done.stdout
    assert pi.head() == before
    (check,) = _checks(pi)
    tree = Path(check.split("cwd=")[1].split(" ")[0])
    assert tree != pi.repo.resolve()
    assert tree.is_relative_to(tmp_path.resolve())
    assert not tree.exists()
    assert check.endswith("config=yes code=yes")
    assert not any(line.startswith(("curl", "uv")) or "restart" in line for line in pi.calls())
    assert not list(pi.repo.glob("**/__pycache__"))


def test_a_dry_run_refuses_a_config_the_new_tree_cannot_load(tmp_path: Path) -> None:
    """Exit 1 with the same line, and still nothing moved."""
    pi = _pi(tmp_path)
    before = pi.head()
    pi.refuse()

    done = pi.run("--dry-run")

    assert done.returncode == 1
    assert done.stdout == ""
    assert done.stderr == (
        f"deploy: {pi.old} -> {pi.new} refused: config does not parse ({REFUSAL})\n"
    )
    assert pi.head() == before
    assert not any("restart" in line for line in pi.calls())


def test_a_dry_run_across_a_lock_change_says_the_config_was_not_checked(tmp_path: Path) -> None:
    """The venv lacks the new dependencies, so a check there would prove nothing."""
    pi = _pi(tmp_path, lock_moves=True)

    done = pi.run("--dry-run")

    assert done.returncode == 0, done.stderr
    assert "uv.lock changed, config not checked (uv.lock changed" in done.stdout
    assert _checks(pi) == []
