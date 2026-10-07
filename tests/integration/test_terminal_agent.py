"""``jarvis terminal-agent`` and the terminal's restart under it (ADR 0183).

Observed through the plist written to the agents dir and the ``launchctl`` argv sequence,
against a fake ``launchctl``. Nothing here touches the real ``gui/$UID`` domain.
"""

from __future__ import annotations

import plistlib
import socket
from pathlib import Path

import pytest

from jarvis.cli import main
from jarvis.deployment import launchd
from jarvis.runtime.terminal import _Speech, _Ui, _ui_device

BRAIN = "http://jarvis:8006"


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """A fake launchctl with a switch for the daemon agent, and every path under tmp."""
    calls: list[tuple[str, ...]] = []
    state = {"daemon_loaded": False}
    gui = launchd.gui_domain()

    def fake_launchctl(*args: str) -> launchd.LaunchctlResult:
        calls.append(args)
        refused = args == ("print", f"{gui}/{launchd.AGENT_LABEL}") and not state["daemon_loaded"]
        return launchd.LaunchctlResult(
            argv=("launchctl", *args), returncode=3 if refused else 0, stdout="", stderr="",
        )

    monkeypatch.setattr(launchd, "_launchctl", fake_launchctl)
    monkeypatch.setattr(launchd, "gui_session_available", lambda: True)
    monkeypatch.setattr(launchd, "validate_interpreter", lambda *_a, **_k: "ok")
    monkeypatch.setattr(launchd, "LAUNCH_AGENTS_DIR_LITERAL", str(tmp_path / "agents"))
    monkeypatch.setattr(launchd, "default_runtime_root", lambda: tmp_path / "root")
    return {"calls": calls, "state": state, "agents": tmp_path / "agents"}


def _plist(rig: dict[str, object]) -> Path:
    return Path(str(rig["agents"])) / f"{launchd.TERMINAL_LABEL}.plist"


def test_install_runs_the_terminal_under_keepalive_and_refuses_while_the_daemon_is_loaded(
    rig: dict[str, object], capsys: pytest.CaptureFixture[str],
) -> None:
    """The plist is the terminal's argv with KeepAlive; a loaded daemon agent stops it first."""
    state = rig["state"]
    calls = rig["calls"]
    assert isinstance(state, dict)
    assert isinstance(calls, list)
    gui = launchd.gui_domain()

    state["daemon_loaded"] = True
    assert main(["terminal-agent", "install", "--brain", f"{BRAIN}/"]) == 1
    assert "com.allen.jarvis is loaded" in capsys.readouterr().err
    assert not _plist(rig).exists()  # refused before a plist was written
    assert all(call[0] == "print" for call in calls)  # and before anything was bootstrapped

    state["daemon_loaded"] = False
    calls.clear()
    assert main(["terminal-agent", "install", "--brain", f"{BRAIN}/"]) == 0
    spec = plistlib.loads(_plist(rig).read_bytes())
    assert spec["Label"] == "com.allen.jarvis.terminal"
    assert spec["ProgramArguments"][1:] == [
        "-m", "jarvis", "terminal", "--brain", BRAIN, "--voice", "--serve-ui",
    ]
    assert spec["KeepAlive"] is True
    assert spec["EnvironmentVariables"][launchd.AGENT_ENV_MARKER] == launchd.TERMINAL_LABEL
    service = f"{gui}/com.allen.jarvis.terminal"
    assert [call for call in calls if call[0] != "print"] == [
        ("bootout", service),
        ("bootstrap", gui, str(_plist(rig))),
        ("enable", service),
    ]
    assert "brain-token" not in _plist(rig).read_text(encoding="utf-8")


def test_uninstall_boots_the_terminal_out_and_removes_its_plist(rig: dict[str, object]) -> None:
    """The way back: the job is stopped, the plist is gone, and the daemon may be installed."""
    assert main(["terminal-agent", "install", "--brain", BRAIN]) == 0
    calls = rig["calls"]
    assert isinstance(calls, list)
    calls.clear()
    assert main(["terminal-agent", "uninstall"]) == 0
    assert not _plist(rig).exists()
    assert calls == [("bootout", f"{launchd.gui_domain()}/com.allen.jarvis.terminal")]
    assert main(["terminal-agent", "uninstall"]) == 0  # nothing left to remove is not a failure


def test_brain_goes_with_install_only(rig: dict[str, object]) -> None:
    """A bad brain URL or a stray flag is refused before launchctl is asked anything."""
    assert main(["terminal-agent", "install", "--brain", "jarvis"]) == 1
    with pytest.raises(SystemExit):
        main(["terminal-agent", "install"])
    with pytest.raises(SystemExit):
        main(["terminal-agent", "uninstall", "--brain", BRAIN])
    assert rig["calls"] == []


def test_the_terminal_restarts_itself_only_under_its_own_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``POST /inherent/restart`` has a handler where KeepAlive respawns the terminal."""
    with socket.socket() as sock:
        ui = _Ui(sock, {}, tmp_path)
        monkeypatch.delenv(launchd.AGENT_ENV_MARKER, raising=False)
        assert _ui_device(ui, None, _Speech).restart is None
        monkeypatch.setenv(launchd.AGENT_ENV_MARKER, launchd.AGENT_LABEL)  # the daemon's agent
        assert _ui_device(ui, None, _Speech).restart is None
        monkeypatch.setenv(launchd.AGENT_ENV_MARKER, launchd.TERMINAL_LABEL)
        assert _ui_device(ui, None, _Speech).restart is not None
