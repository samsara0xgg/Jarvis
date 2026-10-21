"""The runtime's configuration, parsed and validated once (ADR 0370).

``config/jarvis.yaml`` is the shipped file, ``<runtime root>/settings.yaml`` this user's values
over it, and the Settings page's store (ADR 0052) over both. :func:`load_runtime_config` reads
the three, applies the role's overrides (ADR 0170), and returns a :class:`RuntimeConfig`: a
tree of frozen dataclasses that every entry point (``serve``, the terminal, the CLI commands)
builds once and hands down. Nothing below an entry point reads the YAML mapping key by key
again, so a malformed value stops the process before it has opened a database or started a
thread, and a setting has one default.

``raw`` is what is left of the mapping: the merged, role-applied file, for the code that
addresses settings by key path (the Settings page, ADR 0227's drift check) and for the blocks
whose own owner parses them whole (``llm`` in L3, the voice stack's ``realtime`` knobs). Anything
else reads a typed field.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from jarvis.decision import DEFAULT_MAX_TOOL_ITERATIONS
from jarvis.deployment.models import (
    default_sensevoice_dir,
    default_silero_vad_path,
    default_voiceprint_model_path,
)
from jarvis.runtime.settings import SETUP_VOICES, apply_settings
from jarvis.shared import lang
from jarvis.shared.realtime import Wave1FeatureFlags, Wave4ResponseFlags
from jarvis.shared.realtime_trace import record_realtime_trace
from jarvis.state.daily_report import resolve_zone
from jarvis.state.memory_db import MemorySettings, SessionSettings

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import tzinfo

LOGGER = logging.getLogger("jarvis.runtime")

CONFIG_FILENAME: Final = Path("config") / "jarvis.yaml"
PROMPT_FILENAME: Final = Path("prompts") / "jarvis_v1.md"
PRICING_FILENAME: Final = Path("data") / "pricing.json"

Role = Literal["all", "brain"]


class RuntimeBootstrapError(RuntimeError):
    """Raised when the config cannot be loaded or :func:`bootstrap_runtime_app` cannot assemble."""


# What the brain forces off whatever the user's files say: each of these reads or
# drives this machine's own devices, files or apps, and a terminal owns those. The vault and
# the open_path bookmarks are the terminal's alone (ADR 0225): `None` replaces a whole block.
_BRAIN_OVERRIDES: Final[dict[str, Any]] = {
    "observer": {
        "repos": [],
        "timesink": {"enabled": False},
    },
    "realtime": {"ambient_sounds": False, "gpt_live": {"enabled": False}},
    "daily_report": {"codex_sessions": False},
    "tools": {"workers": {"enabled": False}, "obsidian": {"vault_root": ""}, "open_path": None},
}


@dataclass(frozen=True)
class ModelPaths:
    """ADR-0006 §5: where the voice artifacts are, resolved against the config file's directory.

    ``voiceprint_model`` is the speaker model a Mac running alone embeds its voice with
    (ADR 0286); absent, no voiceprint.
    """

    sensevoice_dir: Path
    silero_vad_path: Path
    voiceprint_model: Path


@dataclass(frozen=True)
class ResponseActivation:
    """ADR-0008 Step 2's flag graph, resolved.

    ``flags`` is what runs, ``requested`` what the file asked for. ``reason`` is ``validated``,
    ``not_requested`` or ``realtime_parent_disabled`` (a streaming switch asked for while
    ``realtime.enabled`` is not true, so ``flags`` is all off).
    """

    flags: Wave4ResponseFlags
    requested: Wave4ResponseFlags
    reason: str


@dataclass(frozen=True)
class ConfirmationSettings:
    """The ``confirmation`` block (ADR 0012, 0321).

    ``ttl_ms`` is how long a card waits (the live burn shortens it); ``words_preset`` the
    ``llm.presets`` entry that reads his words against a waiting card, and ``words_timeout_s``
    its one attempt's budget.
    """

    ttl_ms: int
    words_preset: str
    words_timeout_s: float


@dataclass(frozen=True)
class ObserverSettings:
    """The ``observer`` block: what this machine watches and how often.

    ``poll_interval_s`` (ADR-0009 D5) is read twice: the repo observer polls at it, and a turn
    hands the same number to L3 so the Status Board note's stale threshold (3x the poll interval)
    moves with the cadence the daemon actually runs at. ``repos`` are the watched repositories,
    ``~`` expanded (empty: no observer). ``timesink_db_path`` is the local TimeSink store, set only
    when ``observer.timesink.enabled`` is true (ADR 0023) and polled every
    ``timesink_poll_interval_s``. ``usage_poll_interval_s`` is set only when ``observer.usage`` is
    on (ADR 0018). ``claude_sessions`` lets the Agents page read Claude Code's files.
    """

    poll_interval_s: float
    repos: tuple[str, ...]
    timesink_db_path: Path | None
    timesink_poll_interval_s: float
    usage_poll_interval_s: float | None
    claude_sessions: bool


@dataclass(frozen=True)
class SupervisorSettings:
    """The ``supervisor`` block (ADR 0009 D4).

    The sweep closes overdue actions every ``sweep_interval_s``; ``default_budget_s`` is the
    budget of an action that names none.
    """

    sweep_interval_s: float
    default_budget_s: float


@dataclass(frozen=True)
class VoiceSettings:
    """The ``voice`` block.

    ``lead_in_ms`` (ADR 0320) is the median call time from which a tool says its line.
    """

    lead_in_ms: int


@dataclass(frozen=True)
class ToolIterations:
    """``llm.max_tool_iterations`` and ``max_tool_iterations_voice`` (ADR 0060), resolved.

    How many model calls one turn may chain: ``typed`` for a typed turn, ``spoken`` for one he
    spoke (the voice key, else the typed bound).
    """

    typed: int
    spoken: int

    def for_turn(self, *, spoken: bool) -> int:
        """The bound for a spoken or a typed turn."""
        return self.spoken if spoken else self.typed


@dataclass(frozen=True)
class RealtimeSettings:
    """The ``realtime`` block's switches and bounds; its voice knobs stay in ``raw``.

    ``enabled`` is the parent switch (exactly ``true``). ``cancel_timeout_ms`` is the cancel
    connection's SQLite ``busy_timeout``: how long a contended terminal CAS waits, never an
    in-flight provider call. ``input_*`` bound the intent pump (``realtime.input``).
    """

    enabled: bool
    wave1: Wave1FeatureFlags
    response: ResponseActivation
    cancel_timeout_ms: int
    input_queue_capacity: int
    input_max_concurrent_turns: int
    voice_service_tier: str | None


@dataclass(frozen=True)
class RuntimeConfig:
    """Everything the runtime reads from its config, parsed once.

    ``zone`` is the owner's: ``work_state.timezone``, else the machine's own (``zone_name`` is
    its IANA name). ``pricing_path`` is ``<repo>/data/pricing.json``.
    """

    config_path: Path
    config_dir: Path
    repo_root: Path
    prompt_path: Path
    pricing_path: Path
    role: Role
    listen_addresses: tuple[str, ...]
    listen_hosts: tuple[str, ...]
    language: lang.Language
    assistant_name: str
    reply_language: str
    zone: tzinfo
    zone_name: str
    models: ModelPaths
    memory: MemorySettings
    session: SessionSettings
    realtime: RealtimeSettings
    confirmation: ConfirmationSettings
    voice: VoiceSettings
    tool_iterations: ToolIterations
    observer: ObserverSettings
    supervisor: SupervisorSettings
    raw: Mapping[str, Any]


def locate_repo_root(start: Path) -> Path:
    """Walk up from ``start`` until ``config/jarvis.yaml`` exists; return that dir."""
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / CONFIG_FILENAME).is_file():
            return candidate
    msg = (
        f"runtime: could not locate config/jarvis.yaml by walking up from {start}. "
        "Pass config_path=Path(...) explicitly to bootstrap_runtime_app."
    )
    raise RuntimeBootstrapError(msg)


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        msg = f"config at {path} is not a YAML mapping"
        raise RuntimeBootstrapError(msg)
    return raw


def _overlay(base: Mapping[str, Any], top: Mapping[str, Any]) -> dict[str, Any]:
    """``top`` over ``base``: mappings merge key by key, any other value replaces."""
    merged = dict(base)
    for key, value in top.items():
        below = merged.get(key)
        if isinstance(below, dict) and isinstance(value, dict):
            merged[key] = _overlay(below, value)
        else:
            merged[key] = value
    return merged


def _assistant_name(raw: Mapping[str, Any]) -> str:
    """``assistant_name`` — what the user calls the assistant; ``Jarvis`` when unset."""
    name = raw.get("assistant_name")
    return name.strip() if isinstance(name, str) and name.strip() else "Jarvis"


def _named(value: Any, name: str) -> Any:  # noqa: ANN401 — any YAML value.
    """Put the assistant's name in for every ``{assistant}`` in the config's strings."""
    if isinstance(value, str):
        return value.replace("{assistant}", name)
    if isinstance(value, dict):
        return {key: _named(item, name) for key, item in value.items()}
    if isinstance(value, list):
        return [_named(item, name) for item in value]
    return value


def _system_language() -> lang.Language:
    """The first of macOS's preferred languages (then ``$LANG``): Chinese or English."""
    out = ""
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["/usr/bin/defaults", "read", "-g", "AppleLanguages"],
                capture_output=True, text=True, timeout=2, check=False,
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            out = ""
    first = out.strip().strip("()").split(",")[0].strip().strip('"')
    return lang.normalize(first) or lang.normalize(os.environ.get("LANG")) or "en"


def _language(raw: Mapping[str, Any]) -> lang.Language:
    """``language`` from the user's settings, else the system language."""
    chosen_raw = raw.get("language")
    chosen = lang.normalize(chosen_raw)
    if chosen is None and chosen_raw not in (None, ""):
        LOGGER.warning(
            "settings: language %r is not zh or en; using the system language", chosen_raw,
        )
    return chosen or _system_language()


def _role(raw: Mapping[str, Any]) -> Role:
    """``runtime.role``: ``all`` (one machine does everything, the default) or ``brain``."""
    block = raw.get("runtime")
    value = block.get("role", "all") if isinstance(block, dict) else "all"
    if value == "all":
        return "all"
    if value == "brain":
        return "brain"
    msg = f"runtime: runtime.role must be all or brain, not {value!r}"
    raise RuntimeBootstrapError(msg)


def _listen(raw: Mapping[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``runtime.listen_addresses`` and ``runtime.listen_hosts``: where the daemon also answers.

    A brain may have any (ADR 0170), and so may a Mac running alone (ADR 0207), only on private
    addresses: a wildcard or a public address stops the boot, so no setting can open the daemon
    to the internet.
    """
    block = raw.get("runtime")
    block = block if isinstance(block, dict) else {}
    listed = {key: block.get(key) or [] for key in ("listen_addresses", "listen_hosts")}
    for key, value in listed.items():
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            msg = f"runtime: runtime.{key} must be a list of strings"
            raise RuntimeBootstrapError(msg)
    if not listed["listen_addresses"] and not listed["listen_hosts"]:
        return (), ()
    for item in listed["listen_addresses"]:
        try:
            address = ipaddress.ip_address(item)
        except ValueError:
            msg = f"runtime: runtime.listen_addresses has {item!r}, which is not an IP address"
            raise RuntimeBootstrapError(msg) from None
        if address.is_unspecified or address.is_multicast or address.is_global:
            msg = f"runtime: runtime.listen_addresses has {item!r}, which is not a private address"
            raise RuntimeBootstrapError(msg)
    if any(not item or "*" in item for item in listed["listen_hosts"]):
        msg = "runtime: runtime.listen_hosts must be plain host names, no wildcards"
        raise RuntimeBootstrapError(msg)
    return tuple(listed["listen_addresses"]), tuple(listed["listen_hosts"])


def _owner_zone(raw: Mapping[str, Any]) -> tzinfo | None:
    """``work_state.timezone`` — the local zone day windows are cut in; unset = system local."""
    block = raw.get("work_state")
    named = block.get("timezone") if isinstance(block, dict) else None
    if not isinstance(named, str) or not named.strip():
        return None
    try:
        return ZoneInfo(named.strip())
    except ZoneInfoNotFoundError as exc:
        message = f"work_state.timezone must be an IANA zone name: {named!r}"
        raise ValueError(message) from exc


_FALLBACK_CANCEL_TIMEOUT_MS: Final = 500
_FALLBACK_INPUT_QUEUE_CAPACITY: Final = 8
_FALLBACK_INPUT_MAX_CONCURRENT_TURNS: Final = 2


def _positive_int(block: object, *, key: str, fallback: int, where: str) -> int:
    """``<where>.<key>`` as a positive int; anything else is ``fallback``, with one warning."""
    value = block.get(key) if isinstance(block, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        if value is not None:
            LOGGER.warning(
                "%s.%s=%r is not a positive int; using %d", where, key, value, fallback,
            )
        return fallback
    return value


_FALLBACK_CONFIRMATION_TTL_MS: Final = 600_000
_FALLBACK_CONFIRMATION_WORDS_PRESET: Final = "haiku"
_FALLBACK_CONFIRMATION_WORDS_TIMEOUT_S: Final = 4.0
_FALLBACK_LEAD_IN_MS: Final = 2000
_FALLBACK_OBSERVER_POLL_INTERVAL_S: Final = 60.0
_FALLBACK_TIMESINK_POLL_INTERVAL_S: Final = 300.0
_FALLBACK_USAGE_POLL_INTERVAL_S: Final = 300.0
_FALLBACK_SWEEP_INTERVAL_S: Final = 30.0
_FALLBACK_SUPERVISOR_BUDGET_S: Final = 700.0


def _positive_float(value: object, fallback: float) -> float:
    """Coerce a YAML scalar to a positive float; ``fallback`` on anything else.

    ``bool`` is excluded explicitly because it is an ``int`` subclass:
    ``poll_interval_s: true`` would otherwise become a 1-second poll.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return fallback
    return float(value) if value > 0 else fallback


def _confirmation(raw: Mapping[str, Any]) -> ConfirmationSettings:
    """``confirmation``: ``ttl_ms`` (10 min), ``words_preset`` (haiku), ``words_timeout_s`` (4)."""
    block = raw.get("confirmation")
    block = block if isinstance(block, dict) else {}
    ttl = block.get("ttl_ms")
    return ConfirmationSettings(
        ttl_ms=(
            ttl if isinstance(ttl, int) and not isinstance(ttl, bool) and ttl > 0
            else _FALLBACK_CONFIRMATION_TTL_MS
        ),
        words_preset=str(block.get("words_preset") or _FALLBACK_CONFIRMATION_WORDS_PRESET),
        words_timeout_s=_positive_float(
            block.get("words_timeout_s"), _FALLBACK_CONFIRMATION_WORDS_TIMEOUT_S,
        ),
    )


def _voice(raw: Mapping[str, Any]) -> VoiceSettings:
    """``voice.lead_in_ms``: a whole number of milliseconds, else 2000."""
    block = raw.get("voice")
    value = block.get("lead_in_ms") if isinstance(block, dict) else None
    return VoiceSettings(
        lead_in_ms=value if isinstance(value, int) and not isinstance(value, bool)
        else _FALLBACK_LEAD_IN_MS,
    )


def _observer(raw: Mapping[str, Any]) -> ObserverSettings:
    """The ``observer`` block.

    Malformed rows are dropped rather than refused: a typo in a list Allen manages must not stop
    the daemon booting. An enabled TimeSink with no path does stop it.
    """
    block = raw.get("observer")
    block = block if isinstance(block, dict) else {}

    def section(name: str) -> dict[str, Any]:
        value = block.get(name)
        return value if isinstance(value, dict) else {}

    timesink, usage = section("timesink"), section("usage")
    repos = block.get("repos")
    store = None
    if timesink.get("enabled") is True:
        path = timesink.get("db_path")
        if not isinstance(path, str) or not path.strip():
            message = "observer.timesink.db_path must be a nonempty local path"
            raise ValueError(message)
        store = Path(path).expanduser().resolve()
    return ObserverSettings(
        poll_interval_s=_positive_float(
            block.get("poll_interval_s"), _FALLBACK_OBSERVER_POLL_INTERVAL_S,
        ),
        repos=tuple(
            str(Path(item).expanduser())
            for item in (repos if isinstance(repos, list) else ())
            if isinstance(item, str) and item.strip()
        ),
        timesink_db_path=store,
        timesink_poll_interval_s=_positive_float(
            timesink.get("poll_interval_s"), _FALLBACK_TIMESINK_POLL_INTERVAL_S,
        ),
        usage_poll_interval_s=(
            _positive_float(usage.get("poll_interval_s"), _FALLBACK_USAGE_POLL_INTERVAL_S)
            if usage.get("enabled") is True else None
        ),
        claude_sessions=section("claude_sessions").get("enabled") is True,
    )


def _supervisor(raw: Mapping[str, Any]) -> SupervisorSettings:
    """``supervisor.sweep_interval_s`` (30) and ``supervisor.default_budget_s`` (700)."""
    block = raw.get("supervisor")
    block = block if isinstance(block, dict) else {}
    return SupervisorSettings(
        sweep_interval_s=_positive_float(
            block.get("sweep_interval_s"), _FALLBACK_SWEEP_INTERVAL_S,
        ),
        default_budget_s=_positive_float(
            block.get("default_budget_s"), _FALLBACK_SUPERVISOR_BUDGET_S,
        ),
    )


def _tool_iterations(raw: Mapping[str, Any]) -> ToolIterations:
    """``llm.max_tool_iterations`` (ADR 0060) and its voice counterpart.

    Unset or not a positive int keeps decide()'s default; a voice bound unset or unusable is the
    typed bound.
    """
    block = raw.get("llm")
    block = block if isinstance(block, dict) else {}

    def bound(key: str) -> int | None:
        value = block.get(key)
        ok = isinstance(value, int) and not isinstance(value, bool) and value > 0
        return value if ok else None

    typed = bound("max_tool_iterations") or DEFAULT_MAX_TOOL_ITERATIONS
    return ToolIterations(typed=typed, spoken=bound("max_tool_iterations_voice") or typed)


def _voice_service_tier(realtime: dict[str, Any]) -> str | None:
    """``realtime.response.voice_service_tier``, stripped; none unless it is non-empty text."""
    response = realtime.get("response")
    tier = response.get("voice_service_tier") if isinstance(response, dict) else None
    return tier.strip() if isinstance(tier, str) and tier.strip() else None


def _response_activation(realtime: dict[str, Any]) -> ResponseActivation:
    """Resolve the ADR-0008 Step 2 flag graph in exactly one place.

    Every precondition for the Wave-4A switches lives here rather than being re-derived at each
    combination point, which is what made the flags drift (the Wave 0-3 audit). One rule: a
    ``routine_streaming``, ``spoken_streaming`` or ``lifecycle_commentary`` request while
    ``realtime.enabled`` is not true downgrades the graph to all off, with one warning and one
    ``response_activation_downgraded`` trace point per load. The ResponseRun lifecycle and the
    cancel seam are no switches (ADR 0332): bootstrap builds both for every runtime.
    """
    response, commentary = realtime.get("response"), realtime.get("commentary")
    requested = Wave4ResponseFlags.from_mapping(
        response if isinstance(response, dict) else None,
        commentary=commentary if isinstance(commentary, dict) else None,
    )
    if realtime.get("enabled") is not True:
        if requested.all_disabled:
            return ResponseActivation(Wave4ResponseFlags(), requested, "not_requested")
        reason = "realtime_parent_disabled"
        LOGGER.warning(
            "realtime.response downgraded (%s): requested routine_streaming=%s "
            "spoken_streaming=%s lifecycle_commentary=%s; effective all off",
            reason, requested.routine_streaming, requested.spoken_streaming,
            requested.lifecycle_commentary,
        )
        record_realtime_trace(
            "response_activation_downgraded",
            reason=reason,
            requested=(
                f"routine_streaming={requested.routine_streaming},"
                f"spoken_streaming={requested.spoken_streaming},"
                f"lifecycle_commentary={requested.lifecycle_commentary}"
            ),
        )
        return ResponseActivation(Wave4ResponseFlags(), requested, reason)
    return ResponseActivation(
        requested, requested, "not_requested" if requested.all_disabled else "validated",
    )


def _realtime(realtime: dict[str, Any]) -> RealtimeSettings:
    """The ``realtime`` block's switches (``realtime`` is the block, made a dict)."""
    enabled = realtime.get("enabled") is True
    safety = realtime.get("concurrency_safety")
    return RealtimeSettings(
        enabled=enabled,
        wave1=Wave1FeatureFlags.from_mapping(safety if isinstance(safety, dict) else None)
        if enabled else Wave1FeatureFlags(),
        response=_response_activation(realtime),
        cancel_timeout_ms=_positive_int(
            realtime.get("response"), key="cancel_timeout_ms", fallback=_FALLBACK_CANCEL_TIMEOUT_MS,
            where="realtime.response",
        ),
        input_queue_capacity=_positive_int(
            realtime.get("input"), key="queue_capacity", fallback=_FALLBACK_INPUT_QUEUE_CAPACITY,
            where="realtime.input",
        ),
        input_max_concurrent_turns=_positive_int(
            realtime.get("input"), key="max_concurrent_turns",
            fallback=_FALLBACK_INPUT_MAX_CONCURRENT_TURNS, where="realtime.input",
        ),
        voice_service_tier=_voice_service_tier(realtime),
    )


def _model_path(
    raw: Mapping[str, Any], *, key: str, config_dir: Path, fallback: Path,
) -> Path:
    """Return `realtime.<key>` as an absolute path, or ``fallback`` (ADR-0006 §5).

    Resolution rule for every path-valued ``realtime.*`` key: ``~``-expanded,
    and if still relative anchored at the directory holding the config file the
    operator pointed at with ``--config`` — never at ``os.getcwd()``, which is
    the defect this key exists to remove, and never at ``repo_root``, which is
    itself the guess ``config_path.parent.parent``.

    An absent or malformed value degrades to ``fallback`` with one warning and
    never fails boot: a missing artifact is already handled softly by the
    daemon's pre-flight, so a config typo must not be harder to recover from than
    a missing file.
    """
    block = raw.get("realtime")
    if not isinstance(block, dict):
        return fallback
    value = block.get(key)
    if value is None:
        return fallback
    if not isinstance(value, str) or not value.strip():
        LOGGER.warning("realtime.%s must be a non-empty string; using %s.", key, fallback)
        return fallback
    try:
        # `Path.expanduser` RAISES on a `~` it cannot resolve, unlike
        # `os.path.expanduser`, which returns the string untouched. A missing
        # slash -- `~models/silero.onnx` -- is enough, and this runs at the start of
        # every command, not just `serve`.
        candidate = Path(value.strip()).expanduser()
    except RuntimeError:
        LOGGER.warning(
            "realtime.%s (%r) has an unresolvable '~' prefix; using %s.", key, value, fallback,
        )
        return fallback
    if candidate.is_absolute():
        return candidate
    return (config_dir / candidate).resolve()


def load_runtime_config(
    path: Path | None,
    runtime_root: Path,
    *,
    prompt_path: Path | None = None,
) -> RuntimeConfig:
    """The config at ``path`` (the repo's own when ``None``), this runtime root's values over it.

    Reads ``path``, lays ``<runtime_root>/settings.yaml`` over it and then the Settings page's
    store (ADR 0052), fills the assistant's name in (``{assistant}``), and parses the result
    with :func:`parse_runtime_config`. A mapping without a section reads as that section's
    defaults, so a partial file loads.

    Raises:
        RuntimeBootstrapError: the file is missing or not a mapping, or a value is refused.
        ValueError: ``work_state.timezone`` is not an IANA zone name.
    """
    if path is None:
        path = locate_repo_root(Path(__file__).parent) / CONFIG_FILENAME
    if not path.is_file():
        msg = f"runtime: config file {path} does not exist"
        raise RuntimeBootstrapError(msg)
    settings_yaml = runtime_root / "settings.yaml"
    raw = _read_yaml_mapping(path)
    if settings_yaml.is_file():
        raw = _overlay(raw, _read_yaml_mapping(settings_yaml))
    name = _assistant_name(raw)
    raw = apply_settings({key: _named(value, name) for key, value in raw.items()}, runtime_root)
    return parse_runtime_config(
        raw, config_path=path, runtime_root=runtime_root, prompt_path=prompt_path,
    )


def parse_runtime_config(
    raw: Mapping[str, Any],
    *,
    config_path: Path,
    runtime_root: Path,
    prompt_path: Path | None = None,
) -> RuntimeConfig:
    """Parse and validate a merged config mapping; the loader's second half.

    ``config_path`` names the file the mapping came from: its directory anchors the relative
    model paths, the repository is two levels up, and it is the file an error names. The role's
    overrides are applied here (ADR 0170): a brain forces off what drives a device. ``raw`` is
    not changed.
    """
    raw = dict(raw)
    repo_root = config_path.resolve().parent.parent
    name = _assistant_name(raw)
    role = _role(raw)
    listen_addresses, listen_hosts = _listen(raw)
    if role == "brain":
        raw = _overlay(raw, _BRAIN_OVERRIDES)
    language = _language(raw)
    realtime = dict(raw.get("realtime") or {})
    raw["realtime"] = realtime
    if not realtime.get("tts_voice"):
        realtime["tts_voice"] = SETUP_VOICES[language][0]
    zone_name, zone = resolve_zone(None, _owner_zone(raw))
    config_dir = config_path.resolve().parent
    return RuntimeConfig(
        config_path=config_path,
        config_dir=config_dir,
        repo_root=repo_root,
        prompt_path=prompt_path if prompt_path is not None else repo_root / PROMPT_FILENAME,
        pricing_path=repo_root / PRICING_FILENAME,
        role=role,
        listen_addresses=listen_addresses,
        listen_hosts=listen_hosts,
        language=language,
        assistant_name=name,
        reply_language=str(raw.get("reply_language", "follow")),
        zone=zone,
        zone_name=zone_name,
        models=ModelPaths(
            sensevoice_dir=_model_path(
                raw, key="sensevoice_dir", config_dir=config_dir,
                fallback=default_sensevoice_dir(runtime_root),
            ),
            silero_vad_path=_model_path(
                raw, key="silero_vad_path", config_dir=config_dir,
                fallback=default_silero_vad_path(runtime_root),
            ),
            voiceprint_model=_model_path(
                raw, key="voiceprint_model_path", config_dir=config_dir,
                fallback=default_voiceprint_model_path(runtime_root),
            ),
        ),
        memory=MemorySettings.from_config(raw.get("memory"), runtime_root=runtime_root),
        session=SessionSettings.from_config(raw.get("session")),
        realtime=_realtime(realtime),
        confirmation=_confirmation(raw),
        voice=_voice(raw),
        tool_iterations=_tool_iterations(raw),
        observer=_observer(raw),
        supervisor=_supervisor(raw),
        raw=raw,
    )
