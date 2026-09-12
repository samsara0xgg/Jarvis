"""``jarvis daemon install|uninstall|restart`` install BOTH agents (ADR-0015 D3).

Asserts on the observables launchd sees — the two plist files written to
the agents dir and the ``launchctl`` argv sequence — against a fake
``launchctl``. Nothing here touches the real ``gui/$UID`` domain.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from jarvis.deployment import launchd


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """A repo-shaped tmp tree, a fake node, a fake launchctl, and a live GUI session."""
    repo = tmp_path / "repo"
    (repo / "desktop" / "resonance" / "node_modules" / "electron").mkdir(parents=True)
    node = tmp_path / "bin" / "node"
    node.parent.mkdir()
    node.write_text("#!/bin/sh\n", encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    def fake_launchctl(*args: str) -> launchd.LaunchctlResult:
        calls.append(args)
        return launchd.LaunchctlResult(
            argv=("launchctl", *args), returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(launchd, "_launchctl", fake_launchctl)
    monkeypatch.setattr(launchd, "gui_session_available", lambda: True)
    monkeypatch.setattr(launchd, "validate_interpreter", lambda *_a, **_k: "ok")
    return {
        "repo": repo,
        "node": node,
        "calls": calls,
        "agents": tmp_path / "agents",
        "root": tmp_path / "root",
    }


def _install(rig: dict[str, object]) -> launchd.InstallResult:
    return launchd.install(
        interpreter=Path("/venv/bin/python"),
        node=rig["node"],  # type: ignore[arg-type]
        working_directory=rig["repo"],  # type: ignore[arg-type]
        runtime_root=rig["root"],  # type: ignore[arg-type]
        agents_dir=rig["agents"],  # type: ignore[arg-type]
    )


def test_install_writes_both_plists_and_bootstraps_each(rig: dict[str, object]) -> None:
    """Both plists land with the pinned keys; each agent gets bootout → bootstrap → enable."""
    result = _install(rig)
    agents = rig["agents"]
    assert isinstance(agents, Path)

    daemon = plistlib.loads((agents / "com.allen.jarvis.plist").read_bytes())
    surface = plistlib.loads((agents / "com.allen.jarvis.resonance.plist").read_bytes())
    assert daemon["ProgramArguments"] == ["/venv/bin/python", "-m", "jarvis", "serve"]
    assert surface["Label"] == "com.allen.jarvis.resonance"
    assert surface["ProgramArguments"] == [str(rig["node"]), "scripts/launch.mjs"]
    assert surface["WorkingDirectory"] == str(Path(str(rig["repo"])) / "desktop" / "resonance")
    assert surface["KeepAlive"] is True
    assert surface["RunAtLoad"] is True
    assert surface["EnvironmentVariables"]["PATH"].startswith(str(Path(str(rig["node"])).parent))
    assert surface["StandardOutPath"].endswith("logs/resonance.out.log")

    gui = launchd.gui_domain()
    assert rig["calls"] == [
        ("bootout", f"{gui}/com.allen.jarvis"),
        ("bootstrap", gui, str(agents / "com.allen.jarvis.plist")),
        ("enable", f"{gui}/com.allen.jarvis"),
        ("bootout", f"{gui}/com.allen.jarvis.resonance"),
        ("bootstrap", gui, str(agents / "com.allen.jarvis.resonance.plist")),
        ("enable", f"{gui}/com.allen.jarvis.resonance"),
    ]
    assert result.plist_changed
    assert result.resonance_plist_changed

    # Idempotent: identical plists are not rewritten.
    again = _install(rig)
    assert not again.plist_changed
    assert not again.resonance_plist_changed


def test_install_refuses_without_electron_or_node(rig: dict[str, object]) -> None:
    """Missing Electron or node refuses with the fix, before any launchctl call."""
    repo = rig["repo"]
    assert isinstance(repo, Path)
    (repo / "desktop" / "resonance" / "node_modules" / "electron").rmdir()
    with pytest.raises(launchd.SurfaceInvalidError, match="npm ci"):
        _install(rig)
    assert rig["calls"] == []  # refused before any launchctl step

    rig["node"] = repo / "missing-node"
    (repo / "desktop" / "resonance" / "node_modules" / "electron").mkdir()
    with pytest.raises(launchd.SurfaceInvalidError, match="node not found"):
        _install(rig)


def test_install_refuses_while_a_manual_daemon_holds_the_lock(
    rig: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hand-started daemon on the lock would respawn-loop the agent; refuse before writing."""
    monkeypatch.setattr(launchd.process_lock, "is_held", lambda _p: True)
    monkeypatch.setattr(launchd.process_lock, "holder_pid", lambda _p: 4242)
    with pytest.raises(launchd.ManualDaemonRunningError, match="kill 4242"):
        _install(rig)
    agents = rig["agents"]
    assert isinstance(agents, Path)
    assert not agents.exists()  # refused before a plist was written


def test_uninstall_and_restart_cover_both_agents(rig: dict[str, object]) -> None:
    """Restart kickstarts both labels; uninstall boots out both and removes both plists."""
    _install(rig)
    calls = rig["calls"]
    assert isinstance(calls, list)
    calls.clear()
    gui = launchd.gui_domain()

    steps = launchd.restart(agents_dir=rig["agents"])  # type: ignore[arg-type]
    assert [s.argv[1:] for s in steps] == [
        ("kickstart", "-k", f"{gui}/com.allen.jarvis"),
        ("kickstart", "-k", f"{gui}/com.allen.jarvis.resonance"),
    ]

    result = launchd.uninstall(agents_dir=rig["agents"])  # type: ignore[arg-type]
    assert result.plist_removed
    assert result.resonance_plist_removed
    assert [s.argv[1:] for s in result.steps] == [
        ("bootout", f"{gui}/com.allen.jarvis"),
        ("bootout", f"{gui}/com.allen.jarvis.resonance"),
    ]
    with pytest.raises(launchd.LaunchdError, match="not installed"):
        launchd.restart(agents_dir=rig["agents"])  # type: ignore[arg-type]
