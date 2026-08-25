"""L6 launchd residency — the ``com.allen.jarvis`` LaunchAgent (ADR-0009 D1).

Single home for everything the launchd job needs to exist: the plist
render, the on-disk paths, and the three ``launchctl`` verbs the CLI
wraps. ``jarvis/cli/__init__.py`` is a thin caller — it prints what this
module returns and never spells a path or a ``launchctl`` argument
itself. That split is what keeps canary H8 green: the ``~/.jarvis``
literal (via :data:`DEFAULT_RUNTIME_ROOT_LITERAL`) and the
``~/Library/LaunchAgents`` literal both live under ``jarvis/deployment/``.

Why a **user-domain LaunchAgent** (``gui/$UID``) and not a LaunchDaemon
(D1): the daemon needs Allen's GUI session for ``say``, ``osascript``
banners, and the PortAudio wake device (spec §3.7.2). Lifecycle is
therefore login-scoped — with FileVault the daemon is down until Allen
logs in — which is consistent with spec §16.1's "Mac, when awake".

Two pins that look like bugs but are not:

- ``KeepAlive`` is plain ``true``, NOT ``{SuccessfulExit: false}``.
  uvicorn traps SIGTERM and exits 0, so a SuccessfulExit-conditioned
  agent would stay down silently after any stray SIGTERM — exactly the
  residency hole this ADR exists to close. The off switch is
  ``launchctl bootout`` via :func:`uninstall`, which stops the job
  regardless of ``KeepAlive``.
- :func:`install` runs ``[interpreter, "-c", "import jarvis"]`` and
  refuses before writing the plist when it fails. mise/uv shims are not
  on launchd's PATH and a pruned interpreter must be caught at install
  time, not at 3am respawn time; :func:`status` re-checks it.

**No secrets in the plist** — it lands 0644 in ``~/Library/LaunchAgents``.
Keys reach the launchd-spawned daemon through the Step-1 fill-only
``${runtime_root}/env`` loader (:func:`jarvis.deployment.load_env_file`)
instead.

Layer rules (L6): stdlib only plus the ``jarvis.deployment`` siblings
(:data:`DEFAULT_RUNTIME_ROOT_LITERAL`, :mod:`jarvis.deployment.process_lock`).
This module never imports :mod:`jarvis.state` — the H13 exception granted
to ``sleep_wake.py`` does not extend here.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from jarvis.deployment import DEFAULT_RUNTIME_ROOT_LITERAL, process_lock

# The launchd job label. Also the plist basename and the trailing
# component of the service target ``gui/<uid>/<label>``.
AGENT_LABEL = "com.allen.jarvis"

# Per-user LaunchAgents directory. The second (and last) home-relative
# literal this codebase owns; H8 only guards ``~/.jarvis`` but the same
# "paths live in deployment/" rule applies by convention.
LAUNCH_AGENTS_DIR_LITERAL = "~/Library/LaunchAgents"

# launchd never rotates StandardOutPath / StandardErrorPath. ``status``
# warns above this so Allen learns about a runaway log before the disk
# does. 50 MB is weeks of normal daemon chatter.
LOG_SIZE_WARN_BYTES = 50 * 1024 * 1024

# Plist pins (D1).
_THROTTLE_INTERVAL_S = 10
_PLIST_FILE_MODE = 0o644

# Subprocess bounds. The interpreter probe imports the whole jarvis
# package (numpy, fastapi, ...) so it gets a generous budget; launchctl
# calls are local IPC and should never take seconds.
_INTERPRETER_PROBE_TIMEOUT_S = 60.0
_LAUNCHCTL_TIMEOUT_S = 15.0

# ``launchctl print`` emits ``key = value`` lines. We read a handful;
# anything else is ignored.
_PRINT_LINE_RE = re.compile(r"^\s*(?P<key>[a-z][a-z ]*[a-z])\s+=\s+(?P<value>.+?)\s*$")
_PRINT_STATE_KEY = "state"
_PRINT_PID_KEY = "pid"
_PRINT_EXIT_KEYS = ("last exit status", "last exit code")

# ``launchctl bootstrap`` into a domain that does not exist fails with
# errno 5 (EIO). That is the SSH case — a user domain only exists for a
# logged-in window-server session — and D1 requires we say so in words.
_ERRNO_IO_MARKERS = ("Input/output error", ": 5:")


class LaunchdError(RuntimeError):
    """A ``launchctl`` step or a plist precondition failed."""


class InterpreterInvalidError(LaunchdError):
    """``[interpreter, "-c", "import jarvis"]`` did not exit 0."""


class GuiSessionUnavailableError(LaunchdError):
    """No launchd user domain for this uid — the SSH case (D1)."""


@dataclass(frozen=True)
class LaunchctlResult:
    """One ``launchctl`` invocation, captured for reporting and proofs."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        """True iff launchctl exited 0."""
        return self.returncode == 0


@dataclass(frozen=True)
class InstallResult:
    """Outcome of :func:`install`."""

    plist_path: Path
    logs_dir: Path
    interpreter: Path
    plist_changed: bool
    steps: tuple[LaunchctlResult, ...]


@dataclass(frozen=True)
class UninstallResult:
    """Outcome of :func:`uninstall`."""

    plist_path: Path
    plist_removed: bool
    steps: tuple[LaunchctlResult, ...]


@dataclass(frozen=True)
class LogFileStatus:
    """Size probe for one of the two launchd-owned log files."""

    path: Path
    exists: bool
    size_bytes: int

    @property
    def oversized(self) -> bool:
        """True when the file has grown past :data:`LOG_SIZE_WARN_BYTES`."""
        return self.size_bytes > LOG_SIZE_WARN_BYTES


@dataclass(frozen=True)
class DaemonStatus:
    """Everything ``jarvis daemon status`` reports, as data.

    The CLI verb renders this via :func:`format_status` and prints it; it
    computes nothing of its own.
    """

    installed: bool
    plist_path: Path
    service_target: str
    gui_session: bool
    loaded: bool
    state: str | None
    pid: int | None
    last_exit: str | None
    lock_path: Path
    lock_held: bool
    lock_pid: int | None
    interpreter: Path
    interpreter_ok: bool
    interpreter_detail: str
    logs: tuple[LogFileStatus, ...]


def repo_root() -> Path:
    """Repository root — ``jarvis/deployment/launchd.py`` is two parents down."""
    return Path(__file__).resolve().parents[2]


def default_interpreter() -> Path:
    """The venv interpreter launchd should exec (``<repo>/.venv/bin/python``)."""
    return repo_root() / ".venv" / "bin" / "python"


def default_runtime_root() -> Path:
    """Resolved default runtime root — the launchd job never gets ``--runtime-root``."""
    return Path(DEFAULT_RUNTIME_ROOT_LITERAL).expanduser()


def logs_dir(runtime_root: Path | None = None) -> Path:
    """``${runtime_root}/logs`` — created by :func:`install`, written by launchd."""
    root = runtime_root if runtime_root is not None else default_runtime_root()
    return root / "logs"


def launch_agents_dir(override: Path | None = None) -> Path:
    """``~/Library/LaunchAgents`` unless a test / proof overrides it."""
    return override if override is not None else Path(LAUNCH_AGENTS_DIR_LITERAL).expanduser()


def plist_path(agents_dir: Path | None = None) -> Path:
    """Absolute path of the agent's plist file."""
    return launch_agents_dir(agents_dir) / f"{AGENT_LABEL}.plist"


def is_agent_installed(agents_dir: Path | None = None) -> bool:
    """True iff the plist exists — the D2 signal that fork-detach must stay off.

    Presence of the plist (not ``launchctl print`` liveness) is the right
    test: during a crash / respawn window the job is momentarily not
    running, but the CLI must still forward rather than fork.
    """
    return plist_path(agents_dir).is_file()


def gui_domain() -> str:
    """The launchd user domain for the current uid (``gui/501``)."""
    return f"gui/{os.getuid()}"


def service_target() -> str:
    """Fully-qualified service target (``gui/501/com.allen.jarvis``)."""
    return f"{gui_domain()}/{AGENT_LABEL}"


def render_plist(
    *,
    interpreter: Path | None = None,
    working_directory: Path | None = None,
    runtime_root: Path | None = None,
) -> str:
    """Render the LaunchAgent plist XML (D1's pinned key set).

    Args:
        interpreter: ``ProgramArguments[0]``. Defaults to
            :func:`default_interpreter`.
        working_directory: ``WorkingDirectory``. Defaults to the repo
            root — the same cwd :func:`validate_interpreter` probes
            under, so ``import jarvis`` resolves identically at install
            time and at launchd exec time.
        runtime_root: Root whose ``logs/`` holds the two log files.
            Defaults to the resolved ``~/.jarvis``.

    Returns:
        The plist as XML text, ready to write 0644.
    """
    interp = interpreter if interpreter is not None else default_interpreter()
    workdir = working_directory if working_directory is not None else repo_root()
    logs = logs_dir(runtime_root)
    spec: dict[str, object] = {
        "Label": AGENT_LABEL,
        "ProgramArguments": [str(interp), "-m", "jarvis", "serve"],
        "WorkingDirectory": str(workdir),
        "RunAtLoad": True,
        # Plain ``true`` — see the module docstring. Do NOT "improve"
        # this into {"SuccessfulExit": False}.
        "KeepAlive": True,
        "ThrottleInterval": _THROTTLE_INTERVAL_S,
        "StandardOutPath": str(logs / "daemon.out.log"),
        "StandardErrorPath": str(logs / "daemon.err.log"),
    }
    return plistlib.dumps(spec, sort_keys=True).decode("utf-8")


def validate_interpreter(interpreter: Path, *, working_directory: Path | None = None) -> str:
    """Probe ``[interpreter, "-c", "import jarvis"]``; raise when it fails.

    Args:
        interpreter: Candidate ``ProgramArguments[0]``.
        working_directory: cwd for the probe; defaults to the repo root,
            which is also the plist's ``WorkingDirectory``.

    Returns:
        A one-line human detail string on success.

    Raises:
        InterpreterInvalidError: Missing, not executable, timed out, or
            exited non-zero.
    """
    workdir = working_directory if working_directory is not None else repo_root()
    argv = [str(interpreter), "-c", "import jarvis"]
    try:
        proc = subprocess.run(  # noqa: S603 — argv is built here, never from user text.
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=_INTERPRETER_PROBE_TIMEOUT_S,
            cwd=str(workdir),
        )
    except (FileNotFoundError, NotADirectoryError, PermissionError) as exc:
        msg = f"interpreter {interpreter} is not runnable: {exc}"
        raise InterpreterInvalidError(msg) from None
    except subprocess.TimeoutExpired:
        msg = f"interpreter {interpreter} did not finish `import jarvis` in time"
        raise InterpreterInvalidError(msg) from None
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else f"exit {proc.returncode}"
        msg = (
            f"interpreter {interpreter} cannot `import jarvis` (cwd {workdir}): {tail}. "
            "launchd does not see mise / uv shims — point the agent at the venv "
            "interpreter and re-run `jarvis daemon install`."
        )
        raise InterpreterInvalidError(msg)
    return f"{interpreter} imports jarvis (cwd {workdir})"


def _launchctl(*args: str) -> LaunchctlResult:
    """Run one ``launchctl`` subcommand, capturing argv + output."""
    argv = ("launchctl", *args)
    try:
        # S603: fixed argv assembled above, never user text. ``launchctl``
        # resolves via PATH because that is the documented macOS entry point.
        proc = subprocess.run(  # noqa: S603
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=_LAUNCHCTL_TIMEOUT_S,
        )
    except FileNotFoundError:
        msg = "launchctl not found on PATH — launchd residency is macOS-only"
        raise LaunchdError(msg) from None
    except subprocess.TimeoutExpired:
        msg = f"`launchctl {' '.join(args)}` timed out after {_LAUNCHCTL_TIMEOUT_S:.0f}s"
        raise LaunchdError(msg) from None
    return LaunchctlResult(
        argv=argv,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def gui_session_available() -> bool:
    """True iff ``launchctl print gui/$UID`` succeeds (a GUI session exists)."""
    return _launchctl("print", gui_domain()).ok


def gui_session_message() -> str:
    """The human explanation D1 requires instead of a raw errno 5."""
    return (
        f"no launchd user domain for uid {os.getuid()} ({gui_domain()} is unreachable). "
        "This is the SSH case: a user domain only exists for a session logged in at "
        "the Mac's screen. Log in on the machine (or wrap the command in "
        "`launchctl asuser <uid>`) and re-run — installing from a bare SSH shell "
        "cannot work, and launchd would only report `Input/output error`."
    )


def _looks_like_missing_domain(result: LaunchctlResult) -> bool:
    """True when a launchctl failure carries the errno-5 missing-domain shape."""
    blob = f"{result.stderr}\n{result.stdout}"
    return any(marker in blob for marker in _ERRNO_IO_MARKERS)


def install(
    *,
    interpreter: Path | None = None,
    working_directory: Path | None = None,
    runtime_root: Path | None = None,
    agents_dir: Path | None = None,
) -> InstallResult:
    """Validate, write the plist, and (re-)bootstrap the agent. Idempotent.

    Order (D1): validate interpreter, confirm a GUI session, create the
    logs dir, write the plist, ``bootout`` (tolerated: the job may not be
    loaded), ``bootstrap gui/$UID``, ``enable``. Both refusals happen
    BEFORE the plist is written, so a failed install leaves no
    half-configured agent behind.

    Re-running is a no-op in effect: an unchanged plist is not rewritten
    (``plist_changed`` False) and the bootout / bootstrap pair reloads
    the same job definition.

    Args:
        interpreter: Override ``ProgramArguments[0]``.
        working_directory: Override ``WorkingDirectory``.
        runtime_root: Override the root whose ``logs/`` launchd writes.
        agents_dir: Override ``~/Library/LaunchAgents`` (tests / proofs).

    Returns:
        :class:`InstallResult` with the paths touched and every
        ``launchctl`` step executed, in order.

    Raises:
        InterpreterInvalidError: The interpreter cannot import jarvis.
        GuiSessionUnavailableError: No launchd user domain for this uid.
        LaunchdError: ``bootstrap`` or ``enable`` failed for another reason.
    """
    interp = interpreter if interpreter is not None else default_interpreter()
    workdir = working_directory if working_directory is not None else repo_root()

    validate_interpreter(interp, working_directory=workdir)
    if not gui_session_available():
        raise GuiSessionUnavailableError(gui_session_message())

    logs = logs_dir(runtime_root)
    logs.mkdir(parents=True, exist_ok=True)

    target = plist_path(agents_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    rendered = render_plist(
        interpreter=interp,
        working_directory=workdir,
        runtime_root=runtime_root,
    )
    current = target.read_text(encoding="utf-8") if target.is_file() else None
    changed = current != rendered
    if changed:
        target.write_text(rendered, encoding="utf-8")
    target.chmod(_PLIST_FILE_MODE)

    steps: list[LaunchctlResult] = []
    # Tolerated: exits non-zero ("No such process") when nothing is
    # loaded, which is the normal first-install case.
    steps.append(_launchctl("bootout", service_target()))

    boot = _launchctl("bootstrap", gui_domain(), str(target))
    steps.append(boot)
    if not boot.ok:
        if _looks_like_missing_domain(boot):
            raise GuiSessionUnavailableError(gui_session_message())
        msg = (
            f"`launchctl bootstrap {gui_domain()} {target}` failed "
            f"(exit {boot.returncode}): {boot.stderr.strip() or boot.stdout.strip()}"
        )
        raise LaunchdError(msg)

    enable = _launchctl("enable", service_target())
    steps.append(enable)
    if not enable.ok:
        msg = (
            f"`launchctl enable {service_target()}` failed "
            f"(exit {enable.returncode}): {enable.stderr.strip() or enable.stdout.strip()}"
        )
        raise LaunchdError(msg)

    return InstallResult(
        plist_path=target,
        logs_dir=logs,
        interpreter=interp,
        plist_changed=changed,
        steps=tuple(steps),
    )


def uninstall(*, agents_dir: Path | None = None, remove_plist: bool = True) -> UninstallResult:
    """``bootout`` the agent and (by default) delete its plist.

    ``bootout`` stops the job regardless of ``KeepAlive`` — this is the
    documented off switch. A non-zero exit is tolerated: it means the job
    was not loaded, which is a successful uninstall too.

    Args:
        agents_dir: Override ``~/Library/LaunchAgents`` (tests / proofs).
        remove_plist: Delete the plist file as well. False leaves the
            definition on disk (the agent stays "installed" for D2's
            fork-detach guard) while the job is stopped.

    Returns:
        :class:`UninstallResult`.
    """
    steps = (_launchctl("bootout", service_target()),)
    target = plist_path(agents_dir)
    removed = False
    if remove_plist and target.is_file():
        target.unlink()
        removed = True
    return UninstallResult(plist_path=target, plist_removed=removed, steps=steps)


def _parse_launchctl_print(text: str) -> dict[str, str]:
    """Pull the ``key = value`` lines out of ``launchctl print`` output."""
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        match = _PRINT_LINE_RE.match(line)
        if match is None:
            continue
        parsed.setdefault(match.group("key"), match.group("value"))
    return parsed


def _log_status(path: Path) -> LogFileStatus:
    """Size probe for one launchd log file (missing file gives size 0)."""
    try:
        size = path.stat().st_size
    except OSError:
        return LogFileStatus(path=path, exists=False, size_bytes=0)
    return LogFileStatus(path=path, exists=True, size_bytes=size)


def status(
    *,
    interpreter: Path | None = None,
    working_directory: Path | None = None,
    runtime_root: Path | None = None,
    agents_dir: Path | None = None,
) -> DaemonStatus:
    """Collect everything ``jarvis daemon status`` reports.

    Four independent probes, kept separate so a disagreement between them
    is visible rather than averaged away: the plist on disk, ``launchctl
    print`` (state / pid / last exit status), the ``daemon.lock`` holder,
    and the interpreter re-check D1 asks for. Log sizes ride along
    because launchd never rotates those two files.

    Args:
        interpreter: Override the interpreter to re-check.
        working_directory: Override the probe cwd.
        runtime_root: Override the root holding ``daemon.lock`` + ``logs/``.
        agents_dir: Override ``~/Library/LaunchAgents`` (tests / proofs).

    Returns:
        A fully-populated :class:`DaemonStatus`; never raises for a
        missing agent, a dead domain, or a broken interpreter — those are
        reported as fields.
    """
    root = runtime_root if runtime_root is not None else default_runtime_root()
    interp = interpreter if interpreter is not None else default_interpreter()
    target = plist_path(agents_dir)

    gui_ok = gui_session_available()
    printed: dict[str, str] = {}
    loaded = False
    if gui_ok:
        result = _launchctl("print", service_target())
        loaded = result.ok
        if loaded:
            printed = _parse_launchctl_print(result.stdout)

    pid_text = printed.get(_PRINT_PID_KEY)
    pid = int(pid_text) if pid_text is not None and pid_text.isdigit() else None
    last_exit = next((printed[key] for key in _PRINT_EXIT_KEYS if key in printed), None)

    lock_path = root / "daemon.lock"
    interpreter_ok = True
    try:
        interpreter_detail = validate_interpreter(interp, working_directory=working_directory)
    except InterpreterInvalidError as exc:
        interpreter_ok = False
        interpreter_detail = str(exc)

    logs = logs_dir(runtime_root)
    return DaemonStatus(
        installed=target.is_file(),
        plist_path=target,
        service_target=service_target(),
        gui_session=gui_ok,
        loaded=loaded,
        state=printed.get(_PRINT_STATE_KEY),
        pid=pid,
        last_exit=last_exit,
        lock_path=lock_path,
        lock_held=process_lock.is_held(lock_path),
        lock_pid=process_lock.holder_pid(lock_path),
        interpreter=interp,
        interpreter_ok=interpreter_ok,
        interpreter_detail=interpreter_detail,
        logs=(
            _log_status(logs / "daemon.out.log"),
            _log_status(logs / "daemon.err.log"),
        ),
    )


def _format_log_line(log: LogFileStatus) -> str:
    """One ``log`` line, carrying the rotation warning when oversized."""
    if not log.exists:
        return f"log         : {log.path} (absent)"
    megabytes = log.size_bytes / (1024 * 1024)
    warning = (
        "  WARNING: launchd never rotates this file — truncate it or add a "
        "newsyslog.d rule"
        if log.oversized
        else ""
    )
    return f"log         : {log.path} ({megabytes:.1f} MB){warning}"


def format_status(report: DaemonStatus) -> str:
    """Render :func:`status` as the operator-facing block the CLI prints."""
    lines = [
        f"agent       : {report.service_target}",
        f"plist       : {report.plist_path} ({'installed' if report.installed else 'absent'})",
    ]
    if report.gui_session:
        lines.append(f"gui session : {gui_domain()} ok")
        lines.append(f"launchd     : {'loaded' if report.loaded else 'not loaded'}")
        if report.state is not None:
            lines.append(f"state       : {report.state}")
        if report.pid is not None:
            lines.append(f"pid         : {report.pid}")
        if report.last_exit is not None:
            lines.append(f"last exit   : {report.last_exit}")
    else:
        lines.append(f"gui session : UNAVAILABLE — {gui_session_message()}")
    if report.lock_held:
        lines.append(f"daemon.lock : held by pid {report.lock_pid} ({report.lock_path})")
    else:
        lines.append(f"daemon.lock : free ({report.lock_path})")
    ok_word = "ok" if report.interpreter_ok else "BROKEN"
    lines.append(f"interpreter : {ok_word} — {report.interpreter_detail}")
    lines.extend(_format_log_line(log) for log in report.logs)
    return "\n".join(lines)


__all__ = [
    "AGENT_LABEL",
    "DaemonStatus",
    "GuiSessionUnavailableError",
    "InstallResult",
    "InterpreterInvalidError",
    "LaunchctlResult",
    "LaunchdError",
    "LogFileStatus",
    "UninstallResult",
    "format_status",
    "gui_domain",
    "gui_session_available",
    "gui_session_message",
    "install",
    "is_agent_installed",
    "logs_dir",
    "plist_path",
    "render_plist",
    "service_target",
    "status",
    "uninstall",
    "validate_interpreter",
]
