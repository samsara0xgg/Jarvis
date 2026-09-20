"""L6 Deployment — local path bootstrap and runtime-root selection (Mac-only Day-1).

Owns (per spec.html §3.7 and ADR 0001 § Configurable paths, § Six-layer boundary
contract):

- `JARVIS_RUNTIME_ROOT` env var resolution (default `~/.jarvis/`).
- Runtime directory bootstrap: `${root}` and `${root}/artifacts/`.
- Artifact-store placement: per-run subdirectories under `artifacts/`.
- The single canonical location for the literal `"~/.jarvis"` default
  (canary H8 scans `jarvis/` for that string outside this module).

Does NOT own: projection mutation, policy decisions, claim interpretation,
surface rendering. The `jarvis.deployment.__init__` runtime/bootstrap entry
sticks to stdlib only.

Day-2 ADR-0002 § Sleep/wake protocol introduces
`jarvis.deployment.sleep_wake`, which DOES import
`jarvis.state.event_log` to emit `mac.sleeping` / `mac.awake` /
`action.timeout_assumed` events per spec §3.7.8. H13's narrow
exception for `sleep_wake.py` is documented in
`tests/canary/test_layer_ownership_boundaries.py`. The Day-1 stricter
rule "no jarvis.state import" still holds for everything else under
`jarvis/deployment/` (path resolution, runtime root selection,
artifact placement).
"""

from __future__ import annotations

import hmac
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

# Single canonical home for the `~/.jarvis` literal. Every other file in
# `jarvis/` referencing the runtime root must go through `bootstrap_runtime`,
# `RuntimePaths`, or this exported constant. Canary H8 enforces this by AST
# scan of jarvis/ for the literal string outside this module.
DEFAULT_RUNTIME_ROOT_LITERAL = "~/.jarvis"
_ENV_VAR = "JARVIS_RUNTIME_ROOT"


@dataclass(frozen=True)
class RuntimePaths:
    """Resolved, immutable view of the Mac-only Day-1 runtime layout.

    Construction is the caller's responsibility via `bootstrap_runtime`;
    this dataclass itself does no I/O — it is a pure description of where
    things live on disk after bootstrap has run.

    Attributes:
        root: Resolved runtime root (env var or default, expanded + absolute).
        event_log: SQLite Event Log file at `${root}/mac_events.db`
            (spec §5.1 + ADR § Configurable paths). Owned by L2 at write
            time; L6 only places it.
        artifacts_root: Artifact store root at `${root}/artifacts/`.
        registry: File-backed EventTypeRegistry path at `${root}/registry.json`,
            available if Step 4's L2 chooses file-backing. Just a path; L6
            does not create or open it.
        inherent_v2_token: Bearer token for the Inherent realtime v2 socket
            at `${root}/inherent-v2.token` (ADR-0014 D5). L6 owns the
            placement and, via `rotate_inherent_v2_token`, the file's
            permissions; the daemon rewrites it on every boot.
    """

    root: Path
    event_log: Path
    artifacts_root: Path
    registry: Path
    inherent_v2_token: Path

    def pending_write_path(self, confirmation_id: str) -> Path:
        """Return the staging path for a pending write's content (ADR-0012 §3 D3).

        Shape: `${artifacts_root}/pending_writes/<confirmation_id>`.
        The `pending_writes/` directory is created lazily on first call
        (`exist_ok=True`); the content FILE itself is written by the
        caller (`decision/__init__.py`'s `confirm_required` handling),
        not here — this method only guarantees the parent directory
        exists and returns the deterministic path so the same
        `confirmation_id` always resolves to the same artifact.

        Args:
            confirmation_id: The `confirmation_id` minted for one
                `confirmation.requested` ask.

        Returns:
            The path the write's `content` argument should be staged
            at. The parent directory exists after this call.
        """
        pending_dir = self.artifacts_root / "pending_writes"
        pending_dir.mkdir(parents=True, exist_ok=True)
        return pending_dir / confirmation_id


def _resolve_root(root: Path | None) -> Path:
    """Pick the runtime root (explicit arg > env var > built-in default)."""
    if root is not None:
        return root.expanduser().resolve()
    env_value = os.environ.get(_ENV_VAR)
    if env_value:
        return Path(env_value).expanduser().resolve()
    return Path(DEFAULT_RUNTIME_ROOT_LITERAL).expanduser().resolve()


def load_env_file(runtime_root: Path) -> dict[str, str]:
    """Fill-only loader for ``${runtime_root}/env`` (ADR-0009 D1).

    launchd strips Allen's shell environment, so the plist-spawned
    daemon loses ``MINIMAX_API_KEY`` etc.; secrets must not live in the
    world-readable plist. Bootstrap calls this before any surface
    preflight reads the environment.

    Contract:
        - ``KEY=VALUE`` lines, split on the FIRST ``=``; key and value
          are whitespace-stripped, quotes are NOT interpreted.
        - Blank lines and ``#`` comments are skipped; lines without
          ``=`` or with an empty key are skipped.
        - Fill-only: a key already present in ``os.environ`` is never
          overridden (the operator's shell always wins).
        - Missing file → no-op (voice preflight keeps its existing
          text-only degradation).
        - Keyed by ``runtime_root`` — never a hardcoded home path — so
          temp-root test daemons stay isolated from Allen's real keys.

    Args:
        runtime_root: Resolved runtime root whose ``env`` file to load.

    Returns:
        The keys actually applied to ``os.environ`` (fill-only wins
        excluded), for logging / tests.
    """
    env_path = runtime_root / "env"
    if not env_path.is_file():
        return {}
    applied: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip()
        applied[key] = value.strip()
    return applied


def bootstrap_runtime(root: Path | None = None) -> RuntimePaths:
    """Resolve runtime root, create base directories, and return paths.

    Resolution order:
        1. Explicit `root` argument if non-None (test override).
        2. `JARVIS_RUNTIME_ROOT` env var if set and non-empty.
        3. Built-in default `~/.jarvis/` (expanded via `Path.expanduser()`).

    Directory creation:
        - `${root}` created with `parents=True, exist_ok=True`.
        - `${root}/artifacts/` created with `parents=True, exist_ok=True`.
        - `mac_events.db` and `registry.json` are NOT created here;
          they are L2's responsibility at first write.

    Idempotent: repeated calls with the same resolution produce equal
    `RuntimePaths` and do not raise when directories already exist.

    Args:
        root: Optional override (used by tests via `tmp_path`); when
            None, resolves via env var then default.

    Returns:
        Frozen `RuntimePaths` describing the bootstrapped layout.
    """
    resolved_root = _resolve_root(root)
    artifacts_root = resolved_root / "artifacts"

    resolved_root.mkdir(parents=True, exist_ok=True)
    artifacts_root.mkdir(parents=True, exist_ok=True)

    return RuntimePaths(
        root=resolved_root,
        event_log=resolved_root / "mac_events.db",
        artifacts_root=artifacts_root,
        registry=resolved_root / "registry.json",
        inherent_v2_token=resolved_root / "inherent-v2.token",
    )


class InherentTokenError(OSError):
    """The Inherent v2 token file is missing its required safety properties."""


def _require_safe_parent(parent: Path) -> None:
    """Refuse a token directory anyone but its owner could write into."""
    try:
        info = parent.stat()
    except OSError as exc:
        message = f"token directory {parent} is unusable"
        raise InherentTokenError(message) from exc
    if not stat.S_ISDIR(info.st_mode):
        message = f"token directory {parent} is not a directory"
        raise InherentTokenError(message)
    if info.st_uid != os.geteuid():
        message = f"token directory {parent} is not owned by this user"
        raise InherentTokenError(message)
    if info.st_mode & 0o022:
        message = f"token directory {parent} is group- or world-writable"
        raise InherentTokenError(message)


def _require_safe_existing_file(path: Path) -> None:
    """Refuse to replace anything but our own private regular file.

    `lstat` deliberately does not follow symlinks: a link planted at the
    token path would otherwise redirect `os.replace` and hand the secret
    to whoever owns the target.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        message = f"token file {path} is unusable"
        raise InherentTokenError(message) from exc
    if not stat.S_ISREG(info.st_mode):
        message = f"token path {path} is not a regular file"
        raise InherentTokenError(message)
    if info.st_uid != os.geteuid():
        message = f"token file {path} is not owned by this user"
        raise InherentTokenError(message)
    if info.st_mode & 0o177:
        message = f"token file {path} is readable or writable beyond its owner"
        raise InherentTokenError(message)


def rotate_inherent_v2_token(path: Path) -> str:
    """Mint a fresh 256-bit Inherent v2 bearer token at `path` (ADR-0014 D5).

    Called once per daemon boot, so a token recovered from a stale file
    stops working as soon as the daemon restarts. The value is returned to
    the caller and written to disk; it must never reach a log line, a URL,
    an environment variable, or a frame.

    Args:
        path: The token file, normally `RuntimePaths.inherent_v2_token`.

    Returns:
        The new token as 64 lowercase hex characters (no trailing newline;
        the file gets one).

    Raises:
        InherentTokenError: The parent directory or an existing file at
            `path` fails a safety check, and rotating would either leak the
            secret or trust a file this user does not exclusively control.
    """
    _require_safe_parent(path.parent)
    _require_safe_existing_file(path)

    token = secrets.token_hex(32)
    temp_path = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        fd = os.open(
            temp_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, (token + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        temp_path.replace(path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return token


def inherent_v2_token_matches(expected: str, presented: str) -> bool:
    """Compare two bearer tokens without leaking their shared prefix length."""
    return hmac.compare_digest(expected.encode("utf-8"), presented.encode("utf-8"))
