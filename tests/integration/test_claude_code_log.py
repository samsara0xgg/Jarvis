"""ADR 0387: every Claude Code transcript, on the Mac and in the cloud, kept raw on the brain.

Invented transcript files in temporary folders only. The brain is the real intake behind the real
route and device-token guard; the Mac's steps are the real shipper's, sending to that intake; the
cloud side is ``scripts/cloud_log.py`` pushing to a bare repository that stands in for the private
one, with a stand-in ``age`` that seals to a key name:

- a piece is kept under its own name, the same piece again is held, one whose bytes are not the
  ones its name says is refused, and this source alone takes a piece over 16 MiB;
- a ``.jsonl`` file ships its new whole lines and a line still being written waits; any other file
  ships whole once it has been quiet a minute; links are not followed; a file read back from its
  pieces is the file;
- a file written anew starts again at 0; a pass ships the oldest files first up to its cap;
- a snapshot file is left to its live twin when the twin starts and ends the same, ships itself
  otherwise, and a snapshot folder is walked until every file in it is settled, then never again;
- the config reads the parts; a cloud batch goes through the repository to the brain and its
  branch is deleted, an unreadable branch stays and is not retried, and no key means no relay;
- the watcher pushes when the files have been quiet 45 seconds or every 10 minutes while they
  grow; ``install`` hooks it into each start once; the cloud and the Mac cut the same pieces.
"""

from __future__ import annotations

import functools
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml
from fastapi.testclient import TestClient

from jarvis.state import code_log, imports
from jarvis.state.device_tokens import device_name_for_token, device_token_matches, pair_device
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.state.shipments import Shipments, ShipRefused
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server.app import create_app
from jarvis.surface.inherent_server.deps import InherentDeps
from jarvis.surface.inherent_server.guards import require_local_key
from jarvis.surface.mac_ship import ShipConfig, Shipper, ship_config
from jarvis.surface.mac_ship_cloud_log import CloudLogConfig
from jarvis.surface.mac_ship_code_log import KEPT, CodeLogConfig, Tree
from jarvis.surface.mac_ship_core import Conflict, ShipFailed, State
from scripts import cloud_log

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git")

REMOTE = "100.87.250.92"
T0 = 1_792_000_000  # seconds; every file's mtime is set from here, and every pass's clock
MIN = 60_000
SESSION = "cse_01TestSession"
RECIPIENT = "age1" + "q" * 58
STAND_IN_AGE = """\
import sys
mode, _flag, key = sys.argv[1:4]
data = sys.stdin.buffer.read()
if mode == "--encrypt":
    sys.stdout.buffer.write(b"SEALED " + key.encode() + b"\\n" + data[::-1])
else:
    head, _, body = data.partition(b"\\n")
    if head != b"SEALED " + open(key).read().strip().encode():
        sys.exit(1)
    sys.stdout.buffer.write(body[::-1])
"""


def _line(n: int, **more: object) -> bytes:
    return json.dumps({"type": "user", "uuid": f"u-{n}", "n": n, **more}).encode() + b"\n"


def _put(path: Path, data: bytes, at_s: float = T0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (at_s, at_s))
    return path


def _append(path: Path, data: bytes, at_s: float) -> None:
    with path.open("ab") as log:
        log.write(data)
    os.utime(path, (at_s, at_s))


class IntakeSink:
    """The brain's file intake called in-process, as the HTTP route would."""

    def __init__(self, intake: Shipments) -> None:
        """Nothing sent yet."""
        self.intake = intake
        self.sent: list[str] = []

    def send_entries(self, source: str, body: bytes) -> None:
        """No transcript step sends entries."""
        raise AssertionError((source, body))

    def send_file(self, source: str, relpath: str, data: bytes) -> str:
        """Keep one piece; the digest the brain kept."""
        self.sent.append(relpath)
        try:
            return self.intake.receive_file(source, relpath, data).sha256
        except ShipRefused as exc:
            raise (Conflict if exc.status == 409 else ShipFailed)(str(exc)) from None


@dataclass
class Rig:
    """A brain archive, a Mac with two accounts' config folders, and its shipper."""

    root: Path
    archive: Path
    sink: IntakeSink
    a: Path
    b: Path

    @property
    def both(self) -> CodeLogConfig:
        """Both accounts' config folders, no snapshot."""
        return CodeLogConfig((Tree("a", self.a), Tree("b", self.b)))

    def shipper(self, logs: CodeLogConfig | None, cloud: CloudLogConfig | None = None) -> Shipper:
        """The Mac's shipper with only the transcript steps on."""
        config = ShipConfig(self.root / "ship" / "state.db", code_log=logs, cloud_log=cloud)
        return Shipper(config, self.sink, State(config.state_path))

    def held(self, rel: str) -> Path:
        """The folder of one file's pieces on the brain."""
        return self.archive / code_log.SOURCE / "files" / rel

    def names(self, rel: str) -> list[str]:
        """The names of one file's pieces on the brain."""
        folder = self.held(rel)
        return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    """An empty brain archive and a Mac with no transcripts yet."""
    archive = tmp_path / "brain" / imports.IMPORTS_DIRNAME
    intake = Shipments(archive, free_bytes=lambda _p: 100 * 1024**3)
    return Rig(tmp_path, archive, IntakeSink(intake), tmp_path / "claude-a", tmp_path / "claude-b")


def _piece(rel: str, start: int, raw: bytes) -> tuple[str, bytes]:
    return f"{rel}/{code_log.piece_name(start, raw)}", code_log.compress(raw)


# --- the route -----------------------------------------------------------------------------


@pytest.fixture
def remote(tmp_path: Path) -> Iterator[tuple[TestClient, str, Path]]:
    """The brain's app from the tailnet, with a paired Mac's token."""
    root = tmp_path / "brain"
    root.mkdir()
    intake = Shipments(root / imports.IMPORTS_DIRNAME, free_bytes=lambda _p: 100 * 1024**3)
    app = create_app(InherentDeps(
        submit_callable=lambda _t: "T", broadcaster=InherentBroadcaster(),
        device_name=functools.partial(device_name_for_token, root), imports_intake=intake,
    ))
    require_local_key(
        app, functools.partial(local_key_matches, local_key(root)), extra_hosts=[REMOTE],
        device_token_matches=functools.partial(device_token_matches, root),
    )
    token = pair_device(root, "macbook")
    with TestClient(app, base_url=f"http://{REMOTE}:8006", client=(REMOTE, 50000)) as client:
        yield client, token, root / imports.IMPORTS_DIRNAME


def test_the_route_keeps_a_piece_under_its_name_and_refuses_one_that_does_not_match(
    remote: tuple[TestClient, str, Path],
) -> None:
    """201 new, 200 held, 422 for a name the bytes do not match, and 64 MiB for this source."""
    client, token, archive = remote
    auth = {"Authorization": f"Bearer {token}"}
    rel, data = _piece("a/mac/projects/p/s.jsonl", 0, _line(1) + _line(2))

    def put(source: str, path: str, body: bytes) -> int:
        url = f"/inherent/imports/{source}/files/{path}"
        return client.put(url, content=body, headers=auth).status_code

    assert put("claude-code-log", rel, data) == 201
    assert (archive / "claude-code-log" / "files" / rel).read_bytes() == data
    assert put("claude-code-log", rel, gzip.compress(_line(1) + _line(2), mtime=1)) == 200
    wrong, _ = _piece("a/mac/projects/p/s.jsonl", 0, _line(3))
    assert put("claude-code-log", wrong, data) == 422
    torn, torn_data = _piece("a/mac/projects/p/s.jsonl", 0, b'{"half":')
    assert put("claude-code-log", torn, torn_data) == 422
    assert put("claude-code-log", rel.replace("/mac/", "/elsewhere/"), data) == 422
    big_raw = os.urandom(17 * 1024 * 1024)
    big, big_data = _piece("a/mac/projects/p/s/tool-results/out.bin", 0, big_raw)
    assert len(big_data) > 16 * 1024 * 1024
    assert put("claude-code-log", big, big_data) == 201
    assert put("timesink", "2026-10-23/shot.png", big_data) == 413


# --- the Mac's own files -------------------------------------------------------------------


def test_new_whole_lines_ship_and_a_line_still_being_written_waits(rig: Rig) -> None:
    """Whole lines go, the torn last one waits; other files wait a quiet minute; links stay."""
    log = _put(rig.a / "projects" / "p" / "s.jsonl", _line(1) + _line(2) + b'{"type":"us')
    note = _put(rig.a / "projects" / "p" / "s" / "tool-results" / "t.txt", b"out\n\nout\n", T0)
    _put(rig.a / "history.jsonl", _line(9))
    (rig.a / "projects" / "p" / "linked.jsonl").symlink_to(log)
    _put(rig.root / "outside" / "x.jsonl", _line(5))
    (rig.a / "projects" / "elsewhere").symlink_to(rig.root / "outside")
    rig.b.mkdir()
    (rig.b / "projects").symlink_to(rig.a / "projects")  # B's whole folder a link to A's
    shipper = rig.shipper(rig.both)

    first = shipper.poll(T0 * 1000 + 30_000)
    assert first.failed == []
    assert rig.names("a/mac/projects/p/s.jsonl") == [
        code_log.piece_name(0, _line(1) + _line(2)),
    ]
    assert rig.names("a/mac/history.jsonl") == [code_log.piece_name(0, _line(9))]
    assert rig.names("a/mac/projects/p/s/tool-results/t.txt") == []
    assert not rig.held("a/mac/projects/p/linked.jsonl").exists()
    assert not rig.held("a/mac/projects/elsewhere").exists()
    assert not rig.held("b").exists()

    _append(log, b'er","uuid":"u-3"}\n' + _line(4), T0 + 40)
    shipper.poll(T0 * 1000 + 61_000)
    assert rig.names("a/mac/projects/p/s/tool-results/t.txt") == [
        code_log.piece_name(0, note.read_bytes()),
    ]
    assert code_log.assemble(rig.held("a/mac/projects/p/s.jsonl")) == log.read_bytes()
    assert len(rig.names("a/mac/projects/p/s.jsonl")) == 2

    sent = len(rig.sink.sent)
    shipper.poll(T0 * 1000 + 120_000)
    assert len(rig.sink.sent) == sent


def test_a_line_over_48_mib_is_passed_over_and_the_lines_after_it_still_ship(rig: Rig) -> None:
    """No piece may hold it, so the cursor steps past it and the file goes on."""
    huge = b'{"big":"' + b"x" * code_log.MAX_PIECE_RAW + b'"}\n'
    log = _put(rig.a / "projects" / "p" / "s.jsonl", _line(1) + huge + _line(2))
    report = rig.shipper(rig.both).poll(T0 * 1000 + MIN)
    assert report.skipped["claude-code-log/too-large"] == 1
    assert rig.names("a/mac/projects/p/s.jsonl") == sorted([
        code_log.piece_name(0, _line(1)),
        code_log.piece_name(len(_line(1)) + len(huge), _line(2)),
    ])
    _append(log, _line(3), T0 + 10)
    rig.shipper(rig.both).poll(T0 * 1000 + 2 * MIN)
    assert code_log.assemble(rig.held("a/mac/projects/p/s.jsonl")) == _line(1) + _line(2) + _line(3)


def test_a_file_written_anew_starts_again_at_zero(rig: Rig) -> None:
    """A changed first line, or a shorter file, ships from byte 0 under new names."""
    log = _put(rig.a / "projects" / "p" / "s.jsonl", _line(1) + _line(2))
    shipper = rig.shipper(rig.both)
    shipper.poll(T0 * 1000 + MIN)

    _put(log, _line(7) + _line(2) + _line(3), T0 + 10)
    shipper.poll(T0 * 1000 + 2 * MIN)
    _put(log, _line(8), T0 + 20)
    shipper.poll(T0 * 1000 + 3 * MIN)

    assert rig.names("a/mac/projects/p/s.jsonl") == sorted([
        code_log.piece_name(0, _line(1) + _line(2)),
        code_log.piece_name(0, _line(7) + _line(2) + _line(3)),
        code_log.piece_name(0, _line(8)),
    ])


def test_a_pass_ships_the_oldest_files_first_up_to_its_cap(rig: Rig) -> None:
    """With room for one piece a pass, the files go in the order they were last written."""
    for name, at in (("new", 300), ("old", 100), ("mid", 200)):
        _put(rig.a / "projects" / "p" / f"{name}.jsonl", _line(at) * 3, T0 + at)
    shipper = rig.shipper(CodeLogConfig((Tree("a", rig.a),), max_pieces=1))

    order = []
    for n in range(3):
        shipper.poll(T0 * 1000 + (n + 10) * MIN)
        order.append(rig.sink.sent[-1].split("/")[4])
    assert order == ["old.jsonl", "mid.jsonl", "new.jsonl"]
    shipper.poll(T0 * 1000 + 20 * MIN)
    assert len(rig.sink.sent) == 3


def test_a_snapshot_is_left_to_its_live_twin_and_otherwise_ships_itself_once(rig: Rig) -> None:
    """A copy the live file still starts and ends with waits for it; the rest ship as theirs."""
    keep = rig.root / "keep"
    _put(rig.a / "projects" / "p" / "s.jsonl", _line(1) + _line(2) + b'{"torn')
    _put(keep / "a-projects" / "p" / "s.jsonl", _line(1) + _line(2))
    _put(keep / "a-projects" / "p" / "gone.jsonl", _line(5))
    _put(keep / "a-projects" / "p" / "other.jsonl", _line(6))
    _put(rig.a / "projects" / "p" / "other.jsonl", _line(66) + _line(6))
    _put(rig.a / "projects" / "p" / "torn.jsonl", _line(3) + b'{"never-ended')
    _put(keep / "a-projects" / "p" / "torn.jsonl", _line(3) + b'{"never-ended')
    _put(keep / "a-jobs" / "j1" / "transcript.jsonl", _line(7))
    _put(keep / "a-jobs" / "j1" / "has space.py", b"x = 1\n")
    _put(keep / "a-jobs" / "j1" / "tmp" / "repo" / "vendored.py", b"x = 2\n")
    trees = (Tree("a", keep / "a-projects", "projects"), Tree("a", keep / "a-jobs", "jobs-archive"))
    config = CodeLogConfig((Tree("a", rig.a),), trees, max_pieces=1)
    shipper = rig.shipper(config)

    shipper.poll(T0 * 1000 + MIN)  # the oldest name first: the live other.jsonl
    shipper.poll(T0 * 1000 + 2 * MIN)
    state = shipper.state
    assert state.meta(KEPT + str(keep / "a-projects")) is None
    for n in range(3, 9):
        shipper.poll(T0 * 1000 + n * MIN)

    assert rig.names("a/mac/projects/p/s.jsonl") == [code_log.piece_name(0, _line(1) + _line(2))]
    assert rig.names("a/mac/projects/p/gone.jsonl") == [code_log.piece_name(0, _line(5))]
    assert rig.names("a/mac/projects/p/other.jsonl") == sorted([
        code_log.piece_name(0, _line(66) + _line(6)), code_log.piece_name(0, _line(6)),
    ])
    assert rig.names("a/mac/projects/p/torn.jsonl") == [code_log.piece_name(0, _line(3))]
    assert rig.names("a/mac/jobs-archive/j1/transcript.jsonl") == [code_log.piece_name(0, _line(7))]
    assert state.meta(KEPT + str(keep / "a-projects")) is not None
    assert state.meta(KEPT + str(keep / "a-jobs")) is not None
    assert not rig.held("a/mac/jobs-archive/j1/tmp").exists()

    _put(keep / "a-projects" / "p" / "late.jsonl", _line(8))
    shipper.poll(T0 * 1000 + 10 * MIN)
    assert not rig.held("a/mac/projects/p/late.jsonl").exists()


def test_the_config_reads_the_transcript_parts(tmp_path: Path) -> None:
    """Both config folders, three snapshots, 256 MiB a pass, the relay keyed under mac-ship/."""
    shipped = yaml.safe_load((Path(__file__).parents[2] / "config" / "jarvis.yaml").read_text())
    shipped["mac_ship"]["enabled"] = True
    config = ship_config(shipped, tmp_path)
    assert config is not None
    assert config.code_log is not None
    assert [(t.account, t.directory, t.under) for t in config.code_log.live] == [
        ("a", Path("~/.claude").expanduser(), ""), ("b", Path("~/.claude-b").expanduser(), ""),
    ]
    assert [(t.account, t.directory.name, t.under) for t in config.code_log.keep] == [
        ("a", "a-projects", "projects"), ("a", "a-jobs-archive", "jobs-archive"),
        ("b", "b-projects", "projects"),
    ]
    assert config.code_log.max_bytes == 256 * 1024 * 1024
    assert config.cloud_log == CloudLogConfig(
        "https://github.com/samsara0xgg/jarvis-cloud-log.git",
        tmp_path / "mac-ship" / "cloud-log-identity.txt", tmp_path / "mac-ship" / "cloud-log.git",
    )
    shipped["mac_ship"]["claude_code_log"]["enabled"] = False
    off = ship_config(shipped, tmp_path)
    assert off is not None
    assert (off.code_log, off.cloud_log) == (None, None)


# --- the cloud -----------------------------------------------------------------------------


@dataclass
class Cloud:
    """A cloud VM's transcripts and watcher, the relay repository, and the Mac's key for it."""

    claude: Path
    repo: Path
    watcher: cloud_log.Watcher
    relay: CloudLogConfig

    def branches(self) -> list[str]:
        """Every branch of the relay repository."""
        listed = subprocess.run(  # noqa: S603 - the test's own repository
            ["git", "ls-remote", "--heads", str(self.repo)],  # noqa: S607 - the test's git
            capture_output=True, text=True, check=True,
        ).stdout
        return sorted(line.split("\t")[1] for line in listed.splitlines())


@pytest.fixture
def cloud(tmp_path: Path) -> Cloud:
    """A VM with no transcripts yet, an empty relay repository, and the Mac's stand-in key."""
    age = tmp_path / "bin" / "age"
    age.parent.mkdir()
    age.write_text(f"#!{sys.executable}\n{STAND_IN_AGE}")
    age.chmod(0o755)
    repo = tmp_path / "relay.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(repo)], check=True)  # noqa: S603, S607
    claude = tmp_path / "vm" / ".claude"
    config = {"account": "a", "recipient": RECIPIENT, "url": str(repo),
              "claude_dir": str(claude), "age": str(age)}
    watcher = cloud_log.Watcher(config, SESSION, tmp_path / "vm" / cloud_log.HOME_DIR)
    identity = tmp_path / "mac" / "identity.txt"
    identity.parent.mkdir()
    identity.write_text(RECIPIENT + "\n")
    relay = CloudLogConfig(str(repo), identity, tmp_path / "mac" / "cloud-log.git", str(age))
    return Cloud(claude, repo, watcher, relay)


def test_a_cloud_batch_goes_through_the_repository_to_the_brain(rig: Rig, cloud: Cloud) -> None:
    """Pushed sealed, fetched, unsealed, kept as the cloud session's own, its branch deleted."""
    log = _put(cloud.claude / "projects" / "-root-repo" / "s.jsonl", _line(1) + _line(2))
    _put(cloud.claude / "history.jsonl", _line(3))
    assert cloud.watcher.once(T0 * 1000 + MIN) == 2
    assert len(cloud.branches()) == 2  # main, and one batch

    without_key = rig.shipper(None, CloudLogConfig(
        cloud.relay.url, cloud.relay.identity.with_name("none.txt"), cloud.relay.git_dir,
        cloud.relay.age,
    ))
    without_key.poll(T0 * 1000 + MIN)
    assert rig.sink.sent == []

    shipper = rig.shipper(None, cloud.relay)
    assert shipper.poll(T0 * 1000 + MIN).failed == []
    held = f"a/cloud-{SESSION}/projects/-root-repo/s.jsonl"
    assert code_log.assemble(rig.held(held)) == log.read_bytes()
    assert rig.names(f"a/cloud-{SESSION}/history.jsonl") == [code_log.piece_name(0, _line(3))]
    assert cloud.branches() == ["refs/heads/main"]

    foreign = cloud_log.Watcher(
        {"account": "a", "recipient": "age1" + "z" * 58, "url": str(cloud.repo),
         "claude_dir": str(cloud.claude), "age": cloud.relay.age},
        "cse_01Other", cloud.claude.parent / "other-home",
    )
    assert foreign.once(T0 * 1000 + MIN) == 2
    sent = len(rig.sink.sent)
    shipper.poll(T0 * 1000 + 2 * MIN)
    shipper.poll(T0 * 1000 + 3 * MIN)
    assert len(rig.sink.sent) == sent
    assert len(cloud.branches()) == 2  # the unreadable branch stays

    cloud.relay.identity.write_text("age1" + "z" * 58 + "\n")  # the key it was sealed to
    os.utime(cloud.relay.identity, (T0 + 1, T0 + 1))
    shipper.poll(T0 * 1000 + 4 * MIN)
    assert rig.names("a/cloud-cse_01Other/history.jsonl") == [code_log.piece_name(0, _line(3))]
    assert cloud.branches() == ["refs/heads/main"]


def test_the_watcher_pushes_when_quiet_45_seconds_or_every_10_minutes(cloud: Cloud) -> None:
    """A batch waits while the files keep changing, unless the last one is 10 minutes old."""
    log = _put(cloud.claude / "projects" / "p" / "s.jsonl", _line(1), T0)
    start = T0 * 1000
    assert cloud.watcher.once(start + 10_000) == 1  # only 10 s quiet, but nothing sent yet
    _append(log, _line(2), T0 + 50)
    assert cloud.watcher.once(start + 80_000) == 0  # 30 s quiet, sent 35 s ago
    assert cloud.watcher.once(start + 95_000) == 1  # 45 s quiet
    for second in range(120, 600, 30):
        _append(log, _line(second), T0 + second)
        assert cloud.watcher.once(start + second * 1000 + 10_000) == 0
    _append(log, _line(700), T0 + 690)
    assert cloud.watcher.once(start + 95_000 + 10 * MIN) == 1  # 5 s quiet, 10 minutes since
    assert len(cloud.branches()) == 4


def test_install_hooks_the_watcher_into_each_start_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its own copy, its config, a first line in ~/.bashrc and one SessionStart hook, twice run."""
    home = tmp_path / "root"
    (home / ".claude").mkdir(parents=True)
    (home / ".bashrc").write_text('[ -z "$PS1" ] && return\n')
    (home / ".claude" / "settings.json").write_text(json.dumps({"model": "x"}))
    for _ in range(2):
        cloud_log.install(home, "a", RECIPIENT, cloud_log.DEFAULT_URL)

    base = home / cloud_log.HOME_DIR
    script = base / "cloud_log.py"
    assert script.read_bytes() == Path(cloud_log.__file__).read_bytes()
    assert json.loads((base / "config.json").read_text()) == {
        "account": "a", "recipient": RECIPIENT, "url": cloud_log.DEFAULT_URL,
        "claude_dir": str(home / ".claude"),
    }
    bashrc = (home / ".bashrc").read_text().splitlines()
    assert len(bashrc) == 2
    assert f"python3 {script} start" in bashrc[0]
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert settings["model"] == "x"
    assert settings["hooks"]["SessionStart"] == [
        {"hooks": [{"type": "command", "command": f"python3 {script} start"}]},
    ]
    with pytest.raises(SystemExit):
        cloud_log.install(home, "a", "not-a-key", cloud_log.DEFAULT_URL)

    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    cloud_log.start(home)
    assert not (base / "watch.log").exists()


def test_the_cloud_and_the_mac_cut_a_file_into_the_same_pieces(rig: Rig, cloud: Cloud) -> None:
    """Same constants, same cut, same names for the same bytes, so a file reads back once."""
    for name in ("TARGET_PIECE_BYTES", "MAX_PIECE_RAW", "SHA_CHARS", "MAX_PATH_PARTS"):
        assert getattr(cloud_log, name) == getattr(code_log, name)
    long_line = b"x" * (code_log.TARGET_PIECE_BYTES + 5) + b"\n"
    for pending in (b"", b"a", b"a\nb", b"a\nb\n", long_line + b"y\n", long_line[:-1]):
        assert cloud_log.line_cut(pending) == code_log.line_cut(pending)
    for path in ("projects/p/s.jsonl", "history.jsonl", "projects/a b/s.jsonl", "projects/../x",
                 "projects/é/s.jsonl", "/".join(["p"] * 9), "projects//s.jsonl"):
        assert cloud_log.file_parts_ok(path.split("/")) == code_log.file_parts_ok(path.split("/"))
    assert cloud_log.compress(long_line) == code_log.compress(long_line)

    content = _line(1) + _line(2) + _line(3) + long_line + _line(4) + b'{"torn'
    _put(rig.a / "projects" / "p" / "s.jsonl", content)
    _put(cloud.claude / "projects" / "p" / "s.jsonl", content)
    rig.shipper(rig.both).poll(T0 * 1000 + MIN)
    cloud.watcher.once(T0 * 1000 + MIN)
    rig.shipper(None, cloud.relay).poll(T0 * 1000 + MIN)
    assert rig.names("a/mac/projects/p/s.jsonl") == rig.names(
        f"a/cloud-{SESSION}/projects/p/s.jsonl",
    )
    assert len(rig.names("a/mac/projects/p/s.jsonl")) == 3
    digest = hashlib.sha256(content[: content.rfind(b"\n") + 1]).hexdigest()
    assembled = code_log.assemble(rig.held("a/mac/projects/p/s.jsonl"))
    assert hashlib.sha256(assembled).hexdigest() == digest
