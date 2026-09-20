r"""L4 file/folder target resolution for the `open_path` tool.

Pure resolution logic — no event emission, no subprocess side effects
beyond read-only `mdfind` calls, no lifecycle awareness. `open_path`
in :mod:`jarvis.execution.tools` is the only caller; it owns the
`action.result_observed` emission + lifecycle transition + `open`
subprocess call. This module owns only "given a spoken fragment, which
file/folder on disk did Allen mean".

Config: ``config/file_targets.yaml`` (schema documented in-file). Located
by walking up from this module's own file looking for ``config/jarvis.yaml``
— the same strategy :func:`jarvis.runtime._locate_repo_root` uses for
``tier0_patterns.yaml``. This module cannot import ``jarvis.runtime``
(sibling-layer restriction, `.importlinter`: `execution` and `runtime` are
not on the same tier — `runtime` sits ABOVE the middle four and is the only
layer allowed to cross them), so the walk-up is duplicated here rather than
imported.

Query normalization + matching (spec: this module is the whole spec for
these rules — there is no external doc):

1. Normalize: strip a trailing 文件/文件夹/目录 noun, drop the connective
   的, lowercase ASCII (CJK code points are left untouched — there is no
   concept of "case" for them).
2. Tokenize the normalized query into ASCII-alphanumeric runs and CJK runs
   (``re.findall(r"[a-z0-9]+|[\\u4e00-\\u9fff]+", text)``).
3. Bookmark match: an exact normalized-alias == normalized-query match
   wins outright over everything else. Failing that, an alias that is a
   forward substring of the query (`alias in query`) is a candidate,
   longest alias wins. The reverse direction (`query in alias`) is
   deliberately NOT checked — it let a short query like "jarvis" match a
   longer, unrelated alias like "my jarvis backup" on pure length, with
   zero filename evidence backing the choice.
   - If the query IS (up to normalization) just the alias — no ASCII/CJK
     tokens survive once the alias's own tokens are subtracted — the
     bookmark's path is the answer outright (`source="bookmark"`),
     provided it satisfies `target_kind`.
   - If tokens remain (e.g. "jarvis 项目的 claude md 文件" after stripping
     的 and 文件 leaves tokens {jarvis, 项目, claude, md}, minus the
     alias's own {jarvis} = {项目, claude, md}), the bookmark's path
     becomes a PRIORITY search root and resolution falls through to the
     search step below with the alias's tokens excluded from the match
     requirement (`source="search"`).
4. Search: gather candidates from (a) a one-level `iterdir()` scan of
   every search root, every bookmark directory, and any priority root from
   step 3, and (b) `mdfind -onlyin <root> -name <token>` over every search
   root and priority root, where `<token>` is the longest ASCII token when
   one survives, else the longest CJK run — a pure-CJK query would
   otherwise never invoke `mdfind` at all and be limited to whatever the
   one-level scans happen to catch (a file not at a search root's top
   level would be invisible to it). A candidate matches if every ASCII
   token (after any bookmark-alias exclusion) is a case-insensitive
   substring of its filename.
   CJK tokens are matched the same way (substring against the filename)
   but are NOT required — they contribute a ranking bonus instead of
   gating inclusion, and each CJK run is expanded into itself PLUS its
   overlapping 2-grams before matching (`_cjk_match_tokens`) — a merged
   run like "桌面上报告" (from "桌面上的报告" once 的 is dropped) almost
   never appears verbatim in a filename, but its 2-gram "报告" will.
   Rationale for the soft/AND split: Chinese possessive phrasing routinely
   carries a semantically-empty scope word ("jarvis 项目**的** claude md
   文件" — "项目" = "project", describing WHICH claude.md, not naming any
   text that could appear in a real filename); treating every CJK token
   as a hard AND would make that entire common phrasing unresolvable.
   When NO ASCII tokens survive (a CJK-only remainder), at least one CJK
   match token must hit, so an empty token set never vacuously matches
   every file in the search roots.
5. Filter: candidates must satisfy `target_kind` (`is_file` / `is_dir` /
   either) and must resolve to a path under `Path.home()` — anything else
   is silently dropped (safety invariant, not a Limitation Claim; a
   resolver returning something outside home is a bug, not a report-worthy
   miss).
6. Rank surviving candidates by `(open_frequency, exact_stem_match,
   mtime)`, all descending. `open_frequency` counts prior successful
   `open_path` completions for that exact path — `_open_frequencies` does
   ONE `iter_events` pass over the L2 event log building a `path -> count`
   dict (not one scan per candidate: that was O(candidates x log size)
   and measured 3.9s at 2000 log rows x 166 candidates before the fix).
   Best-effort — any read/parse error degrades to an empty dict, never
   raises. `exact_stem_match` is 1 when the candidate's filename stem
   (extension stripped) case-insensitively equals the first surviving
   ASCII token — the common "the query names the file, tokens after are
   the extension/a trailing descriptor" shape (e.g. querying "claude md"
   ranks `CLAUDE.md` — stem "claude" — above `claude_notes.md` — stem
   "claude_notes").

Layer rules (`.importlinter`): stdlib + `yaml` (external) + `jarvis.state`
(L2, permitted from L4). No imports from `jarvis.constitution`,
`jarvis.decision`, `jarvis.surface`, `jarvis.deployment`, `jarvis.runtime`,
`jarvis.cli`.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

import yaml

from jarvis.state.event_log import iter_events

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping, Sequence

LOGGER = logging.getLogger(__name__)


TargetKind = Literal["file", "folder", "any"]
"""Restricts a `resolve()` call to files, folders, or either."""

TargetSource = Literal["bookmark", "search"]
"""Where a `ResolvedTarget` came from — see module docstring step 3."""


@dataclass(frozen=True)
class ResolvedTarget:
    """One resolved file/folder result handed back to `open_path`.

    Attributes:
        path: Absolute path, guaranteed to be under `Path.home()` (see
            module docstring step 5 — anything outside home never reaches
            this dataclass).
        display_name: Human-facing name for the Tier 0 response template's
            `{opened_name}` slot — the bookmark alias for a bookmark hit,
            the filename for a search hit.
        source: `"bookmark"` or `"search"` (module docstring step 3).
    """

    path: Path
    display_name: str
    source: TargetSource


@dataclass(frozen=True)
class FileTargetsConfig:
    """Parsed, `~`-expanded contents of `config/file_targets.yaml`."""

    search_roots: tuple[Path, ...]
    bookmarks: Mapping[str, Path]
    editor_extensions: frozenset[str]
    editor_app: str


# --- Config location + loading (mirrors jarvis.runtime._locate_repo_root) ---

_JARVIS_CONFIG_RELATIVE: Final[Path] = Path("config") / "jarvis.yaml"
"""Repo-root marker file — same one `jarvis.runtime` walks up to find."""

_FILE_TARGETS_RELATIVE: Final[Path] = Path("config") / "file_targets.yaml"

_DEFAULT_EDITOR_APP: Final[str] = "Visual Studio Code"

_EMPTY_CONFIG: Final[FileTargetsConfig] = FileTargetsConfig(
    search_roots=(),
    bookmarks={},
    editor_extensions=frozenset(),
    editor_app=_DEFAULT_EDITOR_APP,
)


class FileTargetsConfigError(ValueError):
    """Raised when `config/file_targets.yaml` exists but is malformed."""


def _locate_repo_root(start: Path) -> Path | None:
    """Walk up from `start` until `config/jarvis.yaml` exists; return that dir.

    Returns `None` instead of raising when no such directory is found —
    `open_path` degrading to "no config, no matches" is preferable to a
    hard crash for a voice-assistant convenience tool (contrast with
    `jarvis.runtime._locate_repo_root`, which raises because a missing
    `jarvis.yaml` there means the WHOLE daemon cannot boot).
    """
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / _JARVIS_CONFIG_RELATIVE).is_file():
            return candidate
    return None


def _parse_config(raw: Mapping[str, Any]) -> FileTargetsConfig:
    """Turn the raw YAML mapping into a validated, `~`-expanded config."""
    search_roots = tuple(
        Path(str(p)).expanduser() for p in raw.get("search_roots", []) or []
    )
    bookmarks_raw = raw.get("bookmarks", {}) or {}
    if not isinstance(bookmarks_raw, dict):
        msg = f"file_targets: 'bookmarks' must be a mapping, got {type(bookmarks_raw).__name__}"
        raise FileTargetsConfigError(msg)
    bookmarks: dict[str, Path] = {}
    for alias, target in bookmarks_raw.items():
        target_path = Path(str(target)).expanduser()
        if not target_path.is_absolute():
            msg = (
                f"file_targets: bookmark {alias!r} target {target!r} is not "
                "absolute after expanduser — a relative path would resolve "
                "against the daemon's CWD instead of a fixed location"
            )
            raise FileTargetsConfigError(msg)
        bookmarks[str(alias)] = target_path
    editor_extensions = frozenset(
        str(ext).lower().lstrip(".") for ext in raw.get("editor_extensions", []) or []
    )
    editor_app = str(raw.get("editor_app", _DEFAULT_EDITOR_APP))
    return FileTargetsConfig(
        search_roots=search_roots,
        bookmarks=bookmarks,
        editor_extensions=editor_extensions,
        editor_app=editor_app,
    )


_CONFIG_LOCK: Final[threading.Lock] = threading.Lock()
_CONFIG_CACHE: FileTargetsConfig | None = None


def load_file_targets_config(*, force_reload: bool = False) -> FileTargetsConfig:
    """Load + cache `config/file_targets.yaml` for the process lifetime.

    Missing file (or unlocatable repo root) -> `_EMPTY_CONFIG` (`open_path`
    then never resolves anything, same "off, not broken" posture as Tier 0's
    missing-file handling). A YAML syntax error or a malformed `bookmarks`
    mapping raises `FileTargetsConfigError` — unlike the missing-file case,
    a file that exists but is wrong should not fail silently.

    Args:
        force_reload: Bypass the cache (tests / smoke scripts that edit the
            config file mid-process). Production code never needs this —
            the file does not change while the daemon is running.
    """
    global _CONFIG_CACHE  # noqa: PLW0603 — process-lifetime cache; a module-level dict-of-one is heavier for no benefit.
    with _CONFIG_LOCK:
        if _CONFIG_CACHE is not None and not force_reload:
            return _CONFIG_CACHE

        repo_root = _locate_repo_root(Path(__file__).parent)
        if repo_root is None:
            _CONFIG_CACHE = _EMPTY_CONFIG
            return _CONFIG_CACHE

        config_path = repo_root / _FILE_TARGETS_RELATIVE
        if not config_path.is_file():
            _CONFIG_CACHE = _EMPTY_CONFIG
            return _CONFIG_CACHE

        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            msg = f"file_targets: {config_path} is not valid YAML: {exc}"
            raise FileTargetsConfigError(msg) from exc
        if raw is None:
            _CONFIG_CACHE = _EMPTY_CONFIG
            return _CONFIG_CACHE
        if not isinstance(raw, dict):
            msg = f"file_targets: top-level YAML must be a mapping, got {type(raw).__name__}"
            raise FileTargetsConfigError(msg)

        config = _parse_config(raw)
        _CONFIG_CACHE = config
        return config


# --- Query normalization + tokenization --------------------------------------

_TRAILING_NOUN_RE: Final[re.Pattern[str]] = re.compile(r"(文件夹|目录|文件)$")
_CONNECTIVE_CHAR: Final[str] = "的"
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+")


def _normalize_query(text: str) -> str:
    """Strip a trailing 文件/文件夹/目录 noun, drop 的, lowercase ASCII."""
    stripped = _TRAILING_NOUN_RE.sub("", text.strip()).strip()
    stripped = stripped.replace(_CONNECTIVE_CHAR, "")
    lowered = "".join(ch.lower() if ch.isascii() else ch for ch in stripped)
    return lowered.strip()


def _tokenize(normalized: str) -> list[str]:
    """Split into ASCII-alphanumeric runs and CJK runs, in appearance order."""
    return _TOKEN_RE.findall(normalized)


# --- Bookmark matching --------------------------------------------------------


def _match_bookmark(
    normalized_query: str, bookmarks: Mapping[str, Path]
) -> tuple[str, Path] | None:
    """Return the best bookmark hit, or None.

    Two tiers, checked in order:
    1. Exact match — `norm_alias == normalized_query` — returns
       immediately. Unambiguous by construction, so the first one found
       (YAML declaration order) wins.
    2. Forward containment only — `norm_alias in normalized_query` — the
       query names the alias plus more (e.g. "jarvis 项目的 claude md
       文件"). Longest alias wins among these ("most specific").

    The reverse direction (`normalized_query in norm_alias`) is
    deliberately NOT checked: it let a short query be swallowed by a
    longer, unrelated alias purely on string containment (e.g. a
    bookmark alias "my jarvis backup" would have matched a query as
    short as "jarvis" with zero evidence the file the query actually
    named lives under that alias's directory).
    """
    best: tuple[str, Path] | None = None
    best_len = -1
    for alias, path in bookmarks.items():
        norm_alias = _normalize_query(alias)
        if not norm_alias:
            continue
        if norm_alias == normalized_query:
            return (alias, path)
        if norm_alias in normalized_query and len(norm_alias) > best_len:
            best = (alias, path)
            best_len = len(norm_alias)
    return best


# --- Candidate gathering (Spotlight + one-level scan) ------------------------

_MDFIND_TIMEOUT_S: Final[float] = 5.0


def _mdfind_candidates(root: Path, token: str) -> list[Path]:
    """Run `mdfind -onlyin <root> -name <token>`; empty list on any failure.

    Defensive by design: Spotlight can be disabled, indexing can be
    mid-flight, or `mdfind` can simply be slow on a huge root — none of
    that should turn "no match yet" into a crash. The one-level scan
    (see `_one_level_scan`) is the fallback that keeps `open_path` useful
    even with Spotlight fully unavailable.
    """
    if not root.is_dir():
        return []
    try:
        proc = subprocess.run(  # noqa: S603 — argv list, no shell.
            ["mdfind", "-onlyin", str(root), "-name", token],  # noqa: S607 — `mdfind` resolved via PATH is intentional.
            capture_output=True,
            text=True,
            timeout=_MDFIND_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOGGER.debug("mdfind failed for root=%s token=%r: %r", root, token, exc)
        return []
    if proc.returncode != 0:
        return []
    return [Path(line) for line in proc.stdout.splitlines() if line.strip()]


def _one_level_scan(root: Path) -> list[Path]:
    """List `root`'s direct children; empty list if unreadable/absent.

    Catches unindexed or dot-adjacent files (e.g. `CLAUDE.md` at a repo
    root) that Spotlight may not surface quickly, per the brief's explicit
    "ALSO do a one-level scan" requirement.
    """
    if not root.is_dir():
        return []
    try:
        return list(root.iterdir())
    except OSError as exc:
        LOGGER.debug("one-level scan failed for root=%s: %r", root, exc)
        return []


def _gather_candidates(
    *,
    search_roots: Sequence[Path],
    bookmark_dirs: Sequence[Path],
    priority_roots: Sequence[Path],
    mdfind_token: str | None,
) -> list[Path]:
    """Union of one-level scans + Spotlight hits, de-duplicated, order-preserving.

    `mdfind_token` is the caller-computed Spotlight query term — the
    longest ASCII token when one exists, else (module docstring step 4)
    the longest CJK run, so a pure-CJK query still reaches `mdfind`
    instead of being limited to whatever the one-level scans catch.
    """
    seen: dict[Path, None] = {}

    def add(paths: list[Path]) -> None:
        for p in paths:
            seen.setdefault(p, None)

    # One-level scan: every search root, every bookmark dir, plus any
    # priority root contributed by a partial bookmark match (module
    # docstring step 3) — priority roots first so a bookmark-scoped
    # search finds its answer without depending on scan order elsewhere.
    for root in (*priority_roots, *search_roots, *bookmark_dirs):
        add(_one_level_scan(root))

    if mdfind_token:
        for root in (*priority_roots, *search_roots):
            add(_mdfind_candidates(root, mdfind_token))

    return list(seen.keys())


# --- Filtering + ranking -------------------------------------------------------

_HOME: Final[Path] = Path.home()


def _kind_matches(path: Path, target_kind: TargetKind) -> bool:
    if target_kind == "file":
        return path.is_file()
    if target_kind == "folder":
        return path.is_dir()
    return path.is_file() or path.is_dir()


def _under_home(path: Path) -> bool:
    """Safety invariant: only paths under `Path.home()` may ever resolve."""
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        return False
    return resolved == _HOME or _HOME in resolved.parents


def _cjk_match_tokens(cjk_runs: Sequence[str]) -> list[str]:
    """Expand each CJK run into itself plus its overlapping 2-grams.

    A merged run (e.g. "桌面上报告", produced when the 的 in "桌面上的报告"
    is dropped during normalization — see `_normalize_query`) rarely
    appears verbatim in a real filename; the run mixes multiple concepts
    run together with no boundary left to split on. 2-gram decomposition
    lets a real sub-concept ("报告") score a match on its own; the whole
    run is kept too so an exact multi-character hit still counts. A
    1-char run has no 2-gram to take — its only "sub-token" is itself.
    """
    tokens: list[str] = []
    for run in cjk_runs:
        tokens.append(run)
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def _passes_token_filter(
    filename_lower: str,
    filename: str,
    *,
    mandatory_ascii: Sequence[str],
    bonus_cjk: Sequence[str],
) -> tuple[bool, int]:
    """Apply the AND-over-ASCII / soft-CJK matching rule (module docstring step 4).

    Returns `(passes, cjk_score)` — `cjk_score` feeds nowhere into the
    official `(frequency, exact_match, mtime)` rank tuple (module
    docstring step 6 deliberately keeps that a 3-tuple), it is only used
    here to reject an all-CJK remainder that matches nothing.
    """
    if not all(tok in filename_lower for tok in mandatory_ascii):
        return False, 0
    cjk_score = sum(1 for tok in bonus_cjk if tok in filename)
    if not mandatory_ascii and bonus_cjk and cjk_score == 0:
        return False, 0
    return True, cjk_score


def _open_frequencies(conn: sqlite3.Connection) -> dict[str, int]:
    """Count prior successful `open_path` completions, keyed by `opened_path`.

    ONE `iter_events` pass over the whole L2 event log building a
    `path -> count` dict — NOT one scan per candidate (that was
    O(candidates x log size); measured 3.9s at 2000 log rows x 166
    candidates before this fix, and real logs only grow). Every
    `open_path` success emits `action.result_observed` whose
    `tool_output` is a JSON blob carrying `opened_path` (see
    `jarvis.execution.tools.open_path`). This is a ranking
    signal only — any read/parse failure (missing table, malformed
    JSON, closed connection, ...) degrades to an empty dict (every
    candidate's frequency becomes 0) rather than propagating.
    """
    counts: dict[str, int] = {}
    try:
        for evt in iter_events(conn):
            if evt.type != "action.result_observed":
                continue
            tool_output = evt.payload.get("tool_output")
            if not isinstance(tool_output, str):
                continue
            try:
                parsed = json.loads(tool_output)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            opened_path = parsed.get("opened_path")
            if isinstance(opened_path, str):
                counts[opened_path] = counts.get(opened_path, 0) + 1
    except Exception:  # noqa: BLE001 — best-effort ranking signal; must never break resolve().
        return {}
    return counts


def _rank(
    candidates: Sequence[Path],
    *,
    mandatory_ascii: Sequence[str],
    freq_by_path: Mapping[str, int],
) -> Path:
    """Pick the best candidate by `(open_frequency, exact_stem_match, mtime)`, all desc.

    `freq_by_path` is `_open_frequencies(conn)`'s output — computed ONCE
    per `resolve()` call by the caller, not per candidate here.
    """
    first_token = mandatory_ascii[0] if mandatory_ascii else None

    def sort_key(cand: Path) -> tuple[int, int, float]:
        freq = freq_by_path.get(str(cand), 0)
        exact = 1 if first_token is not None and cand.stem.lower() == first_token else 0
        try:
            mtime = cand.stat().st_mtime
        except OSError:
            mtime = 0.0
        return (freq, exact, mtime)

    return max(candidates, key=sort_key)


# --- Public resolve() ----------------------------------------------------------


def resolve(
    query: str,
    target_kind: TargetKind,
    conn: sqlite3.Connection,
) -> ResolvedTarget | None:
    """Resolve a spoken file/folder fragment to a concrete path, or None.

    See the module docstring for the full normalization / matching /
    ranking rules. Never raises for a bad/empty query or an unresolvable
    one — those simply return `None`; `open_path` turns that into
    a `target_not_found` error result.
    """
    config = load_file_targets_config()

    normalized = _normalize_query(query)
    tokens = _tokenize(normalized)
    if not tokens:
        return None

    ascii_tokens = [t for t in tokens if t.isascii()]
    cjk_tokens = [t for t in tokens if not t.isascii()]

    mandatory_ascii = ascii_tokens
    cjk_runs = cjk_tokens
    priority_roots: list[Path] = []

    bookmark_hit = _match_bookmark(normalized, config.bookmarks)
    if bookmark_hit is not None:
        alias, bookmark_path = bookmark_hit
        alias_tokens = set(_tokenize(_normalize_query(alias)))
        mandatory_ascii = [t for t in ascii_tokens if t not in alias_tokens]
        cjk_runs = [t for t in cjk_tokens if t not in alias_tokens]

        if not mandatory_ascii and not cjk_runs:
            # The query IS the bookmark (nothing left to resolve further).
            if _kind_matches(bookmark_path, target_kind) and _under_home(bookmark_path):
                return ResolvedTarget(path=bookmark_path, display_name=alias, source="bookmark")
            return None

        # Query names something INSIDE the bookmark — scope the search to
        # it (module docstring step 3, second bullet).
        priority_roots = [bookmark_path]

    # module docstring step 4: 2-gram-expand the CJK remainder for the
    # soft bonus match; keep the un-expanded runs for the mdfind fallback
    # below (a bigram is too short to be a useful Spotlight query term).
    bonus_cjk = _cjk_match_tokens(cjk_runs)

    longest_ascii_token = max(mandatory_ascii, key=len) if mandatory_ascii else None
    longest_cjk_run = max(cjk_runs, key=len) if cjk_runs else None
    mdfind_token = longest_ascii_token if longest_ascii_token is not None else longest_cjk_run

    candidates = _gather_candidates(
        search_roots=config.search_roots,
        bookmark_dirs=list(config.bookmarks.values()),
        priority_roots=priority_roots,
        mdfind_token=mdfind_token,
    )

    scored: list[Path] = []
    for cand in candidates:
        if not _kind_matches(cand, target_kind):
            continue
        if not _under_home(cand):
            continue
        passes, _cjk_score = _passes_token_filter(
            cand.name.lower(),
            cand.name,
            mandatory_ascii=mandatory_ascii,
            bonus_cjk=bonus_cjk,
        )
        if passes:
            scored.append(cand)

    if not scored:
        return None

    freq_by_path = _open_frequencies(conn)
    best = _rank(scored, mandatory_ascii=mandatory_ascii, freq_by_path=freq_by_path)
    return ResolvedTarget(path=best, display_name=best.name, source="search")


# --- write-target resolution (ADR-0012 D1) -----------------------------------


def _bookmark_relative_target(query: str, config: FileTargetsConfig) -> Path | None:
    """Resolve `"<bookmark alias>/<relative path>"` to an absolute `Path`.

    Exact-alias-match shorthand for a WRITE target, e.g.
    `"jarvis/scratch/shopping.txt"` with a `jarvis` bookmark. Both
    sides are normalized the same way `_match_bookmark` normalizes
    them, but this checks for equality only — no substring / fuzzy
    matching, unlike `resolve()`'s existing-file search. Returns
    `None` when `query` has no `/`, the head does not exactly match
    any bookmark, or the tail is empty.
    """
    head, sep, rest = query.partition("/")
    if not sep or not rest:
        return None
    normalized_head = _normalize_query(head)
    if not normalized_head:
        return None
    for alias, path in config.bookmarks.items():
        if _normalize_query(alias) == normalized_head:
            return path / rest
    return None


def _parent_in_scope(parent: Path, config: FileTargetsConfig) -> bool:
    """True iff `parent` equals, or is nested under, a configured scope dir.

    "Configured scope" is `search_roots` plus every bookmark target
    directory (ADR-0012 D1: "a non-existent target resolves iff its
    parent directory resolves within `search_roots`/bookmarks scope").
    """
    try:
        resolved_parent = parent.resolve()
    except OSError:
        return False
    for root in (*config.search_roots, *config.bookmarks.values()):
        try:
            resolved_root = root.expanduser().resolve()
        except OSError:
            continue
        if resolved_parent == resolved_root or resolved_root in resolved_parent.parents:
            return True
    return False


def resolve_write_target(query: str, conn: sqlite3.Connection) -> ResolvedTarget | None:
    """Resolve a `write_file` target (ADR-0012 D1 — resolve-on-propose extended for write targets).

    An EXISTING file resolves exactly as :func:`resolve` already does
    (bookmark/search matching over real candidates on disk) — called
    first, unchanged. A NON-existent target resolves only when a
    prospective absolute path can be derived from `query` AND that
    path's parent directory is itself in scope (see
    :func:`_parent_in_scope`); the prospective path then becomes the
    canonical target (this function never creates it — the handler
    does the actual write). Two shapes produce a prospective path:

    1. `"<bookmark alias>/<relative path>"` — exact alias match
       (:func:`_bookmark_relative_target`).
    2. An absolute (or `~`-relative) path.

    A bare natural-language phrase matching neither shape (e.g.
    "scratch 的 shopping 文件" with no bookmark literally named
    "scratch") cannot be turned into a prospective path and returns
    `None` — D1 does not specify a fuzzy new-filename inference
    algorithm, and guessing one risks writing to the wrong place,
    which is worse than refusing (ADR-0011 D3's fail-closed posture:
    an unresolved ref simply refuses at the gate). Never raises.
    """
    existing = resolve(query, "file", conn)
    if existing is not None:
        return existing

    config = load_file_targets_config()
    prospective = _bookmark_relative_target(query, config)
    source: TargetSource = "bookmark"
    if prospective is None:
        candidate = Path(query).expanduser()
        if candidate.is_absolute() and candidate.name:
            prospective = candidate
            source = "search"
    if prospective is None:
        return None
    if not _under_home(prospective):
        return None
    if not _parent_in_scope(prospective.parent, config):
        return None
    return ResolvedTarget(path=prospective, display_name=prospective.name, source=source)


__all__ = [
    "FileTargetsConfig",
    "FileTargetsConfigError",
    "ResolvedTarget",
    "TargetKind",
    "TargetSource",
    "load_file_targets_config",
    "resolve",
    "resolve_write_target",
]
