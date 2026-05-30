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


def _resolve_root(root: Path | None) -> Path:
    """Pick the runtime root (explicit arg > env var > built-in default)."""
    if root is not None:
        return root.expanduser().resolve()
    env_value = os.environ.get(_ENV_VAR)
    if env_value:
        return Path(env_value).expanduser().resolve()
    return Path(DEFAULT_RUNTIME_ROOT_LITERAL).expanduser().resolve()


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
