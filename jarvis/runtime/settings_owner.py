"""ADR 0227: every setting has one declared owner, and a split deployment checks it.

A brain and its terminals each read their own ``settings.yaml`` and settings page store over the
shipped ``config/jarvis.yaml`` (ADR 0170, 0052). :data:`OWNERS` says which side reads each key:

* ``brain``: only the brain does; ``device``: only a terminal (or a machine running alone) does;
* ``both``: each side reads it for its own part, so it is the user's to keep equal;
* ``machine``: each process's own wiring (``runtime.role``), never compared.

A terminal's ``hello`` reports the brain-owned and shared keys its own files set
(:func:`terminal_report`). :class:`SettingsDrift` on the brain compares them with its own, raises
one notice for the terminal's companion when they differ, and acts only on the owner's choice.
"""

from __future__ import annotations

import io
import json
import logging
import re
import shutil
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Literal

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.util import load_yaml_guess_indent

from jarvis.runtime.settings import PATHS, SETTINGS_FILE, put_paths, saved_paths
from jarvis.shared import lang
from jarvis.state import terminal_settings
from jarvis.state.plugin_settings import write_private_json
from jarvis.surface.terminal_link import SETTINGS_CHOICES, SETTINGS_NOTICE

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from pathlib import Path

LOGGER = logging.getLogger(__name__)

type Owner = Literal["brain", "device", "both", "machine"]

_TABLE: Final[dict[Owner, tuple[str, ...]]] = {
    "brain": (
        "llm", "attachments", "supervisor", "work_state.preset", "work_state.auto",
        "daily_report.preset", "daily_report.at", "day_summary", "ledger", "core_memory", "home",
        "job_mail", "moment", "spend_cap", "dashboard", "agents", "jev_log", "tools.web",
        "tools.screen.vision_preset", "tools.plugins", "tools.mcp", "confirmation",
        "diagnostics.log_llm_io", "dictation.polish_preset", "push", "session", "reply_language",
        "onboarded",
        # What the brain's speech provider and turn pipeline read (the terminal only relays).
        "realtime.tts_voice", "realtime.tts_volume", "realtime.tts_model",
        "realtime.tts_primary_endpoint", "realtime.tts_fallback_endpoint",
        "realtime.tts_sample_rate_in_hz", "realtime.tts_connect_timeout_s",
        "realtime.tts_task_start_timeout_s", "realtime.tts_first_chunk_timeout_s",
        "realtime.tts_between_chunk_timeout_s", "realtime.tts_total_timeout_s",
        "realtime.tts_session_close_timeout_s", "realtime.commentary",
        "realtime.surrogate_route", "realtime.jev_words", "realtime.jev_oneshot",
        "realtime.input", "realtime.inherent",
    ),
    "device": (
        "observer.repos", "observer.timesink", "observer.claude_sessions",
        "daily_report.codex_sessions", "tools.obsidian", "tools.open_path", "tools.screen",
        "tools.workers", "dictation", "night", "respeaker",
        # This machine's microphone, speaker, wake word, recognizer and player.
        "realtime.ambient_sounds", "realtime.output_device", "realtime.input_device",
        "realtime.playback_volume", "realtime.final_asr", "realtime.final_asr_language",
        "realtime.final_asr_terms", "realtime.wake_threshold", "realtime.wake_join_timeout_s",
        "realtime.tts_ring_seconds", "realtime.vad", "realtime.single_audio_ingress",
        "realtime.gpt_live", "realtime.sensevoice_dir", "realtime.silero_vad_path",
    ),
    "both": (
        "assistant_name", "language", "projects", "memory", "observer.poll_interval_s",
        "observer.usage", "work_state.timezone", "diagnostics.record_tts_audio",
        "realtime.enabled", "realtime.concurrency_safety", "realtime.response",
        "realtime.streaming_output",
    ),
    "machine": ("runtime",),
}
OWNERS: Final[dict[str, Owner]] = {
    prefix: owner for owner, prefixes in _TABLE.items() for prefix in prefixes
}
"""Dotted key-path prefix to owner; the longest prefix of a key decides."""

PRESENT: Final = "<set>"
"""What stands for a value that is not reported: a secret-looking key's, or an oversized one."""
_SECRET: Final = re.compile(r"key|token|secret|pass|cred|auth", re.IGNORECASE)
_MAX_REPORTED: Final = 200
_MAX_VALUE_CHARS: Final = 2000  # the same caps terminal_link holds a hello's settings to
_BRAIN_SIDE: Final = frozenset({"brain", "both"})
_ASKED: Final = "asked"
_YAML_WIDTH: Final = 4096  # never re-wrap a long line of the owner's
_LISTED: Final = 6  # key names a log line or a card line spells out


def owner_of(path: str) -> Owner | None:
    """The owner of the dotted key ``path``, or ``None`` when no prefix of it is declared."""
    parts = path.split(".")
    for end in range(len(parts), 0, -1):
        owner = OWNERS.get(".".join(parts[:end]))
        if owner is not None:
            return owner
    return None


def page_keys(*owners: Owner) -> frozenset[str]:
    """The Settings page keys (:data:`~jarvis.runtime.settings.PATHS`) owned by ``owners``."""
    return frozenset(key for key, path in PATHS.items() if owner_of(".".join(path)) in owners)


def _flat(node: object, prefix: str = "") -> dict[str, Any]:
    """The leaves of a YAML mapping as dotted paths; a list or an empty mapping is one leaf."""
    if isinstance(node, dict) and node:
        found: dict[str, Any] = {}
        for key, value in node.items():
            found.update(_flat(value, f"{prefix}.{key}" if prefix else str(key)))
        return found
    return {prefix: node} if prefix else {}


def _yaml(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return raw if isinstance(raw, dict) else {}


def user_paths(settings_yaml: Path, root: Path) -> dict[str, Any]:
    """Every leaf this machine's own files set: ``settings.yaml``, the page store over it.

    The shipped YAML is not here: only what the user (or the page) chose.
    """
    return {**_flat(_yaml(settings_yaml)), **saved_paths(root)}


def leaf_paths(config: Mapping[str, Any]) -> set[str]:
    """Every leaf of ``config`` as a dotted path (for the coverage test and the brain's reads)."""
    return set(_flat(dict(config)))


def check_own(side: Literal["brain", "terminal"], settings_yaml: Path, root: Path) -> list[str]:
    """Warn once about the keys in this machine's own files that belong to the other side.

    A brain ignores ``device`` keys; a terminal ignores ``brain`` ones (it reports them to the
    brain instead). Names only, never values. Returns the names.
    """
    foreign: Owner = "device" if side == "brain" else "brain"
    found = sorted(path for path in user_paths(settings_yaml, root) if owner_of(path) == foreign)
    if found:
        LOGGER.warning(
            "settings: %s-owned keys in this %s's own files are ignored here: %s",
            foreign, side, ", ".join(found),
        )
    return found


def _shown(path: str, value: object) -> object:
    if _SECRET.search(path):
        return PRESENT
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return PRESENT
    return value if len(text) <= _MAX_VALUE_CHARS else PRESENT


def terminal_report(settings_yaml: Path, root: Path) -> dict[str, Any]:
    """The ``settings`` of a terminal's hello: its own files' brain-side keys and plain values.

    A key that looks like a secret is reported as :data:`PRESENT`, never its value.
    """
    mine = user_paths(settings_yaml, root)
    paths = [path for path in sorted(mine) if owner_of(path) in _BRAIN_SIDE][:_MAX_REPORTED]
    return {path: _shown(path, mine[path]) for path in paths}


# --- the files -----------------------------------------------------------------------------


def _backup(path: Path) -> None:
    """Copy ``path`` beside itself before it changes; a missing file needs none."""
    if path.is_file():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        shutil.copy2(path, path.with_name(f"{path.name}.bak-{stamp}"))


def _edit_yaml(path: Path, edit: Callable[[dict[str, Any]], None]) -> None:
    """Apply ``edit`` to the file's round-trip tree and write it back, after a backup.

    Comments, key order, quoting and indentation of every line ``edit`` leaves alone stay as the
    owner wrote them.
    """
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    tree, indent, seq_offset = load_yaml_guess_indent(text) if text.strip() else (None, 2, 0)
    tree = CommentedMap() if tree is None else tree
    edit(tree)
    writer = YAML()
    writer.preserve_quotes = True
    writer.width = _YAML_WIDTH
    writer.indent(mapping=indent or 2, sequence=indent or 2, offset=seq_offset or 0)
    out = io.StringIO()
    writer.dump(tree, out)
    _backup(path)
    staged = path.with_suffix(".yaml.tmp")
    staged.write_text(out.getvalue(), encoding="utf-8")
    staged.replace(path)


def _set(tree: dict[str, Any], path: str, value: object) -> None:
    *parents, leaf = path.split(".")
    node = tree
    for part in parents:
        child = node.get(part)
        if not isinstance(child, dict):
            child = node[part] = {}
        node = child
    node[leaf] = value


def _drop(tree: dict[str, Any], path: str) -> None:
    """Remove the leaf ``path`` and the mappings it leaves empty."""
    *parents, leaf = path.split(".")
    chain = [tree]
    for part in parents:
        child = chain[-1].get(part)
        if not isinstance(child, dict):
            return
        chain.append(child)
    chain[-1].pop(leaf, None)
    for part, node, parent in zip(reversed(parents), reversed(chain[1:]), reversed(chain[:-1]),
                                  strict=True):
        if node:
            break
        del parent[part]


def release(settings_yaml: Path, root: Path) -> list[str]:
    """Take the brain-owned keys out of this machine's own files (each backed up first).

    Called on a terminal once the owner has chosen, so a setting then has one owner. Returns the
    removed key names.
    """
    gone = sorted(path for path in user_paths(settings_yaml, root) if owner_of(path) == "brain")
    in_yaml = [path for path in gone if path in _flat(_yaml(settings_yaml))]
    if in_yaml:

        def drop(tree: dict[str, Any]) -> None:
            for path in in_yaml:
                _drop(tree, path)

        _edit_yaml(settings_yaml, drop)
    page = root / SETTINGS_FILE
    saved = _read_json(page)
    kept = {key: value for key, value in saved.items() if _page_owner(key) != "brain"}
    if kept != saved:
        _backup(page)
        write_private_json(page, kept)
    if gone:
        LOGGER.info("settings: released %d brain-owned key(s) from this machine's files", len(gone))
    return gone


def _page_owner(key: str) -> Owner | None:
    return owner_of(".".join(PATHS[key])) if key in PATHS else None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


# --- the brain's side ------------------------------------------------------------------------


def _lookup(config: Mapping[str, Any], path: str) -> Any:  # noqa: ANN401 — a config value.
    node: Any = config
    for part in path.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return node


def _same(reported: object, own: object) -> bool:
    """Equal values; a :data:`PRESENT` stand-in is equal to any value the brain has."""
    return own is not None if reported == PRESENT else reported == own


def _names(keys: Iterable[str]) -> str:
    listed = sorted(keys)
    more = f" (+{len(listed) - _LISTED})" if len(listed) > _LISTED else ""
    return ", ".join(listed[:_LISTED]) + more


class SettingsDrift:
    """What differs between a terminal's own files and the brain's, and the owner's answer.

    Nothing is ever copied by itself: a difference becomes one notice for that device's
    companion, and only the choice on it changes a file.
    """

    def __init__(self, root: Path, settings_yaml: Path, config: Mapping[str, Any]) -> None:
        """Bind the runtime root, the brain's ``settings.yaml`` and the config it booted with."""
        self._root = root
        self._yaml = settings_yaml
        self._config = config
        self._lock = threading.Lock()

    def on_report(self, device: str, reported: Mapping[str, Any]) -> None:
        """A hello's settings: raise the notice unless these differences were already asked."""
        reported = {
            path: value for path, value in reported.items() if owner_of(path) in _BRAIN_SIDE
        }
        differs = {
            path: value for path, value in reported.items()
            if not _same(value, _lookup(self._config, path))
        }
        with self._lock:
            row = terminal_settings.rows(self._root).get(device, {})
            raised = bool(differs) and differs != row.get(_ASKED)
            if raised:
                LOGGER.warning(
                    "settings: terminal %s has brain-side settings that differ from the "
                    "brain's: %s", device, _names(differs),
                )
            row = {
                "reported": dict(reported),
                _ASKED: differs,
                "open": raised or (bool(differs) and bool(row.get("open"))),
                "at": datetime.now(UTC).isoformat() if raised else row.get("at", ""),
            }
            terminal_settings.keep(self._root, device, row)

    def notices(self) -> list[dict[str, Any]]:
        """The waiting cards as ``GET /inherent/notices`` serves them; each names its device."""
        out = []
        for device, row in sorted(terminal_settings.rows(self._root).items()):
            asked = row.get(_ASKED)
            if not row.get("open") or not isinstance(asked, dict) or not asked:
                continue
            line = lang.t("settings.drift.line", device=device, n=len(asked), keys=_names(asked))
            out.append({
                "id": f"{SETTINGS_NOTICE}{device}",
                "kind": "mail",
                "title": lang.t("settings.drift.title"),
                "line": line,
                "level": "card",
                "text": line,
                "at": row.get("at", ""),
                "company": "",
                "role": "",
                "event_at": None,
                "mail_kind": "settings",
                "device": device,
                "keys": sorted(asked),
                "choices": [
                    {"id": choice, "label": lang.t(f"settings.drift.{choice}")}
                    for choice in SETTINGS_CHOICES
                ],
            })
        return out

    def act(self, notice_id: str, action: str, reaction: str | None) -> bool:
        """The owner's answer. ``True`` when a file changed and a restart brings it in.

        ``device`` writes the terminal's values into this brain's files (``settings.yaml`` backed
        up first); ``brain`` keeps the brain's. Either closes the card, and any other
        reaction or dismissing only closes it. An unknown id is a ``LookupError``.
        """
        device = notice_id.removeprefix(SETTINGS_NOTICE)
        with self._lock:
            row = terminal_settings.rows(self._root).get(device)
            if row is None:
                msg = f"no such notice: {notice_id}"
                raise LookupError(msg)
            if action == "seen":
                return False
            changed = False
            if action == "feedback" and reaction == "device":
                changed = self._write(row.get(_ASKED) or {})
            terminal_settings.keep(self._root, device, {**row, "open": False})
        LOGGER.info("settings: terminal %s differences settled: %s", device, reaction or action)
        return changed

    def _write(self, values: Mapping[str, Any]) -> bool:
        wanted = {
            path: value for path, value in values.items()
            if value != PRESENT and owner_of(path) in _BRAIN_SIDE
        }
        rest = put_paths(self._root, wanted)
        if rest:

            def put(tree: dict[str, Any]) -> None:
                for path, value in rest.items():
                    _set(tree, path, value)

            _edit_yaml(self._yaml, put)
        if wanted:
            LOGGER.info("settings: took the terminal's values for %s", _names(wanted))
        return bool(wanted)


__all__ = [
    "OWNERS",
    "PRESENT",
    "SettingsDrift",
    "check_own",
    "owner_of",
    "page_keys",
    "release",
    "terminal_report",
    "user_paths",
]
