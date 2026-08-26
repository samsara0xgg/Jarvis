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
`worker.suspended_by_sleep` / `worker.terminated_by_sleep` /
`action.timeout_assumed` events per spec §3.7.8. H13's narrow
exception for `sleep_wake.py` is documented in
`tests/canary/test_layer_ownership_boundaries.py`. The Day-1 stricter
rule "no jarvis.state import" still holds for everything else under
`jarvis/deployment/` (path resolution, runtime root selection,
artifact placement).
"""

from __future__ import annotations

import os
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
        artifacts_root: Worker artifact store root at `${root}/artifacts/`.
            Per-run subdirs derived via `artifact_dir_for_run`.
        registry: File-backed EventTypeRegistry path at `${root}/registry.json`,
            available if Step 4's L2 chooses file-backing. Just a path; L6
            does not create or open it.
    """

    root: Path
    event_log: Path
    artifacts_root: Path
    registry: Path

    def artifact_dir_for_run(self, run_id: str) -> Path:
        """Return (and create) the per-run artifact directory.

        Shape: `${artifacts_root}/run_<run_id>/`. The directory is created
        lazily on the first call for a given `run_id`; subsequent calls are
        idempotent (`exist_ok=True`). L4 `spawn_worker` writes its real
        artifact (e.g. `diff.json`) into this directory.

        Args:
            run_id: Canonical run identifier from the Event Log
                (`run.started.run_id`).

        Returns:
            The per-run directory path. Always exists after this call.
        """
        run_dir = self.artifacts_root / f"run_{run_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def pending_write_path(self, confirmation_id: str) -> Path:
        """Return the staging path for a pending write's content (ADR-0012 §3 D3).

        Shape: `${artifacts_root}/pending_writes/<confirmation_id>`.
        Sibling of `artifact_dir_for_run`'s `run_<run_id>/` convention.
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
    )
