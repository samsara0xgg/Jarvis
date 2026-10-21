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
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from jarvis.decision import DEFAULT_MAX_TOOL_ITERATIONS
from jarvis.decision.jev_oneshot import TOOL_GROUPS
from jarvis.deployment.models import (
    default_sensevoice_dir,
    default_silero_vad_path,
    default_voiceprint_model_path,
)
from jarvis.execution import maps_spend
from jarvis.execution.tools import (
    DEFAULT_SCREEN_MAX_WIDTH_PX,
    DEFAULT_WEB_FETCH_MAX_BYTES,
    DEFAULT_WEB_FETCH_MAX_TEXT_BYTES,
    DEFAULT_WEB_SEARCH_MAX_RESULTS,
    DEFAULT_WEB_TIMEOUT_S,
)
from jarvis.runtime.day_plan import day_ends_of
from jarvis.runtime.locate import MAX_AGE_S, PUSH_WAIT_S, SOCKET_WAIT_S
from jarvis.runtime.settings import SETUP_VOICES, apply_settings
from jarvis.shared import lang
from jarvis.shared.realtime import Wave1FeatureFlags, Wave4ResponseFlags
from jarvis.shared.realtime_trace import record_realtime_trace
from jarvis.state.daily_report import resolve_zone
from jarvis.state.memory_db import MemorySettings, SessionSettings

if TYPE_CHECKING:
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
    ``ambient_sounds`` (ADR 0151) puts sound labels in the state block.
    """

    enabled: bool
    wave1: Wave1FeatureFlags
    response: ResponseActivation
    cancel_timeout_ms: int
    input_queue_capacity: int
    input_max_concurrent_turns: int
    voice_service_tier: str | None
    ambient_sounds: bool


@dataclass(frozen=True)
class WorkStateSettings:
    """The ``work_state`` block (ADR 0023, 0180).

    ``preset`` is the ``llm.presets`` entry the on-demand analysis runs on (the daily report and
    the mail summaries fall back to it); ``auto`` re-analyses in the background on a change.
    """

    preset: str
    auto: bool


@dataclass(frozen=True)
class DailyReportSettings:
    """The ``daily_report`` block (ADR 0024, 0101).

    ``preset`` is the report's ``llm.presets`` entry; ``at`` the time the daemon writes
    yesterday's report, none when unset or unreadable; ``codex_sessions`` opts in to reading
    Codex's own session directory.
    """

    preset: str
    at: time | None
    codex_sessions: bool


@dataclass(frozen=True)
class LocateSettings:
    """The ``locate`` block (ADR 0281).

    How long Here waits for his phone's fresh fix over the socket and over a push, the name of
    the paired device he carries (empty: the newest), and the age past which a report is stale.
    """

    socket_wait_s: float
    push_wait_s: float
    phone: str
    max_age_s: int


@dataclass(frozen=True)
class DashboardSettings:
    """``dashboard.view.enabled`` (ADR 0176) and ``dashboard.mail.enabled`` (ADR 0147)."""

    view: bool
    mail: bool


@dataclass(frozen=True)
class DiagnosticsSettings:
    """The ``diagnostics`` switches (ADR 0120); each is on only when exactly ``true``."""

    log_llm_io: bool
    record_tts_audio: bool


@dataclass(frozen=True)
class WebToolsSettings:
    """``tools.web`` (ADR-0011 D7): the bounds `web_search` and `web_fetch` share.

    ``search_provider`` is the named provider, stripped; empty picks by which key the
    environment holds. ``search_api_key_env`` names the variable the key is read from, empty to
    derive it from the provider. The key itself is never in the config.
    """

    search_max_results: int
    fetch_max_bytes: int
    fetch_max_text_bytes: int
    timeout_s: float
    search_provider: str
    search_api_key_env: str


@dataclass(frozen=True)
class WorkersSettings:
    """``tools.workers`` (ADR 0019): Codex workers, off unless enabled.

    ``roots`` are the folders a worker may be started in, ``~`` expanded and resolved.
    """

    enabled: bool
    roots: tuple[Path, ...]


@dataclass(frozen=True)
class ToolsSettings:
    """The ``tools`` block's built-in tools.

    ``obsidian_vault_root`` is `search_notes`' vault, ``~`` expanded (none registers no
    `search_notes`); ``vision_preset`` and ``screen_max_width_px`` are `screen_look`'s
    (ADR-0011 D7). ``open_path`` is the bookmarks mapping L4's resolver parses whole (a malformed
    one fails the boot there). The `mcp` and `plugins` blocks stay in ``raw`` for their owners.
    """

    obsidian_vault_root: Path | None
    web: WebToolsSettings
    vision_preset: str
    screen_max_width_px: int
    workers: WorkersSettings
    open_path: Mapping[str, Any]


@dataclass(frozen=True)
class SurrogateRouteSettings:
    """``realtime.surrogate_route`` (ADR 0122): Jev picks a Tier 0 row for a line, or none."""

    model: str
    min_confidence: float
    timeout_ms: int
    parallel: bool
    zdr: bool


@dataclass(frozen=True)
class TurnEndAsksSettings:
    """``agents.turn_end_asks`` (ADR 0125): the bars for a turn's closing question or choice."""

    model: str
    at: float
    question_at: float
    timeout_ms: int


@dataclass(frozen=True)
class VoiceWordsSettings:
    """``realtime.jev_words`` (ADR 0130): Jev reads the control words said inside a sentence."""

    model: str
    at: float
    timeout_ms: int
    max_chars: int


@dataclass(frozen=True)
class JevOneShotSettings:
    """``realtime.jev_oneshot`` (ADR 0139, 0140): one request per voice line, and its readers.

    ``relation_at`` and ``tool_at`` are None while that reader is off; ``words_at`` is None
    while ``realtime.jev_words`` is off, and the words' timeout and length are then their
    defaults.
    """

    model: str
    timeout_ms: int
    zdr: bool
    relation_at: float | None
    window_s: float
    tool_at: float | None
    tool_groups: frozenset[str]
    words_at: float | None
    words_timeout_ms: int
    words_max_chars: int


@dataclass(frozen=True)
class JevSettings:
    """Jev's routes: the decision log (on unless ``jev_log.enabled`` is false) and four routes.

    A route is None while its block is absent or not enabled.
    """

    log: bool
    surrogate_route: SurrogateRouteSettings | None
    turn_end_asks: TurnEndAsksSettings | None
    voice_words: VoiceWordsSettings | None
    oneshot: JevOneShotSettings | None


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
    work_state: WorkStateSettings
    daily_report: DailyReportSettings
    locate: LocateSettings
    home_weather: Mapping[str, Any] | None
    transit_places: Mapping[str, str]
    maps_monthly_usd: float
    dashboard: DashboardSettings
    diagnostics: DiagnosticsSettings
    tools: ToolsSettings
    voiceprint_gate: bool
    remind_before_min: int
    plan_day_ends: time
    jev: JevSettings
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
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        # The problem and its line only: the excerpt a YAMLError carries is the file's own text.
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark is not None else ""
        problem = getattr(exc, "problem", None) or "syntax"
        msg = f"config at {path} is not valid YAML: {problem}{where}"
        raise RuntimeBootstrapError(msg) from exc
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


def system_language() -> lang.Language:
    """The language fixed text is written in when no config could be read."""
    return _system_language()


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


_FALLBACK_WORK_STATE_PRESET: Final = "gpt6-luna"
_MAX_LOCATE_WAIT_S: Final = 30
_MINUTES_IN_A_DAY: Final = 24 * 60
_FALLBACK_REMIND_BEFORE_MIN: Final = 60


def _block(raw: Mapping[str, Any], name: str) -> dict[str, Any]:
    """The ``name`` block of the config; empty when it is not a mapping."""
    value = raw.get(name)
    return value if isinstance(value, dict) else {}


def _switch(block: Mapping[str, Any], key: str, *, where: str) -> bool:
    """``<where>.<key>``, on unless false; anything but a bool is a ``TypeError``."""
    value = block.get(key, True)
    if not isinstance(value, bool):
        msg = f"{where}.{key} must be true or false"
        raise TypeError(msg)
    return value


def _work_state(raw: Mapping[str, Any], config_path: Path) -> WorkStateSettings:
    """``work_state.preset`` (a blank one is the fallback) and ``work_state.auto``.

    A non-bool ``auto`` stops the boot: a background analysis must not run on a guess.
    """
    block = _block(raw, "work_state")
    preset = block.get("preset")
    auto = block.get("auto", False)
    if not isinstance(auto, bool):
        msg = f"runtime: {config_path} work_state.auto must be true or false"
        raise RuntimeBootstrapError(msg)
    return WorkStateSettings(
        preset=preset if isinstance(preset, str) and preset.strip()
        else _FALLBACK_WORK_STATE_PRESET,
        auto=auto,
    )


def _daily_report(raw: Mapping[str, Any], work_state: WorkStateSettings) -> DailyReportSettings:
    """``daily_report``: the preset (else the work-state one), ``at`` (HH:MM) and the opt-in."""
    block = _block(raw, "daily_report")
    preset = block.get("preset")
    at = None
    if (named := block.get("at")) is not None:
        try:
            at = time.fromisoformat(str(named))
        except ValueError:
            LOGGER.warning("daily_report.at %r is not HH:MM; no daily report is written", named)
    return DailyReportSettings(
        preset=preset if isinstance(preset, str) and preset.strip() else work_state.preset,
        at=at,
        codex_sessions=block.get("codex_sessions") is True,
    )


def _locate(raw: Mapping[str, Any]) -> LocateSettings:
    """``locate``: the two waits (seconds, above 0 and up to 30), ``phone`` and ``max_age_s``."""
    block = _block(raw, "locate")
    waits = []
    for key, default in (("socket_wait_s", SOCKET_WAIT_S), ("push_wait_s", PUSH_WAIT_S)):
        value = block.get(key, default)
        if (
            isinstance(value, bool) or not isinstance(value, int | float)
            or not 0 < value <= _MAX_LOCATE_WAIT_S
        ):
            msg = f"locate: locate.{key} is seconds, above 0 and up to {_MAX_LOCATE_WAIT_S}"
            raise RuntimeBootstrapError(msg)
        waits.append(float(value))
    phone, max_age = block.get("phone", ""), block.get("max_age_s", MAX_AGE_S)
    if not isinstance(phone, str):
        msg = "locate: locate.phone is the name of a paired device, or empty"
        raise RuntimeBootstrapError(msg)
    if isinstance(max_age, bool) or not isinstance(max_age, int) or max_age <= 0:
        msg = "locate: locate.max_age_s is a whole number of seconds above 0"
        raise RuntimeBootstrapError(msg)
    return LocateSettings(
        socket_wait_s=waits[0], push_wait_s=waits[1], phone=phone.strip(), max_age_s=max_age,
    )


def _home_weather(raw: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """``home.weather``: where the home's forecast is for; its readers take the mapping."""
    place = _block(raw, "home").get("weather")
    return place if isinstance(place, dict) else None


def _transit_places(
    raw: Mapping[str, Any], weather: Mapping[str, Any] | None,
) -> Mapping[str, str]:
    """``transit.home`` and ``transit.school`` (an address or ``lat,lng``), read once at boot.

    They are copied into the places table (ADR 0234); after that the table is where `bus` reads
    them. An unset ``home`` falls back to the ``home.weather`` latitude and longitude (ADR 0189).
    """
    saved = _block(raw, "transit")
    places = {k: v for k in ("home", "school") if (v := str(saved.get(k) or "").strip())}
    if "home" not in places and weather is not None:
        places["home"] = f"{weather['latitude']},{weather['longitude']}"
    return MappingProxyType(places)


def _maps_monthly_usd(raw: Mapping[str, Any]) -> float:
    """``maps.monthly_usd`` (ADR 0283): the month's cap on Google map spend, USD.

    A missing or unusable value is the default; 0 switches the cap off.
    """
    value = _block(raw, "maps").get("monthly_usd")
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return maps_spend.DEFAULT_MONTHLY_USD
    return float(value)


def _dashboard(raw: Mapping[str, Any]) -> DashboardSettings:
    """``dashboard.<page>.enabled``: off unless true; the mail page needs the view (ADR 0176)."""
    block = _block(raw, "dashboard")

    def enabled(page: str) -> bool:
        switch = block.get(page)
        return isinstance(switch, dict) and switch.get("enabled") is True

    view, mail = enabled("view"), enabled("mail")
    if mail and not view:
        LOGGER.warning("dashboard.mail.enabled needs dashboard.view.enabled: the mail page is off")
    return DashboardSettings(view=view, mail=mail)


def _diagnostics(raw: Mapping[str, Any]) -> DiagnosticsSettings:
    """``diagnostics.log_llm_io`` and ``diagnostics.record_tts_audio`` (ADR 0120)."""
    block = _block(raw, "diagnostics")
    return DiagnosticsSettings(
        log_llm_io=block.get("log_llm_io") is True,
        record_tts_audio=block.get("record_tts_audio") is True,
    )


_FALLBACK_VISION_PRESET: Final = "vision"


def _positive_number(value: object, fallback: float) -> float:
    """``value`` when it is a number above 0; else ``fallback``."""
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return fallback
    return value


def _web_tools(tools: Mapping[str, Any]) -> WebToolsSettings:
    """``tools.web.*``: UX knobs, so a malformed value is the shipped default, not a refusal."""
    block = _block(tools, "web")

    def count(key: str, fallback: int) -> int:
        value = block.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        return fallback

    provider, key_env = block.get("search_provider"), block.get("search_api_key_env")
    return WebToolsSettings(
        search_max_results=count("search_max_results", DEFAULT_WEB_SEARCH_MAX_RESULTS),
        fetch_max_bytes=count("fetch_max_bytes", DEFAULT_WEB_FETCH_MAX_BYTES),
        fetch_max_text_bytes=count("fetch_max_text_bytes", DEFAULT_WEB_FETCH_MAX_TEXT_BYTES),
        timeout_s=float(_positive_number(block.get("timeout_s"), DEFAULT_WEB_TIMEOUT_S)),
        search_provider=provider.strip() if isinstance(provider, str) else "",
        search_api_key_env=key_env.strip() if isinstance(key_env, str) else "",
    )


def _workers(tools: Mapping[str, Any]) -> WorkersSettings:
    """``tools.workers.enabled`` (exactly ``true``) and ``tools.workers.roots``."""
    block = _block(tools, "workers")
    enabled = block.get("enabled") is True
    return WorkersSettings(
        enabled=enabled,
        roots=tuple(Path(str(r)).expanduser().resolve() for r in block.get("roots") or ())
        if enabled else (),
    )


def _tools(raw: Mapping[str, Any]) -> ToolsSettings:
    """The ``tools`` block's built-in tools (ADR-0011 D7)."""
    tools = _block(raw, "tools")
    vault = _block(tools, "obsidian").get("vault_root")
    screen = _block(tools, "screen")
    preset, width = screen.get("vision_preset"), screen.get("max_width_px")
    return ToolsSettings(
        obsidian_vault_root=Path(vault).expanduser() if isinstance(vault, str) and vault.strip()
        else None,
        web=_web_tools(tools),
        vision_preset=preset if isinstance(preset, str) and preset.strip()
        else _FALLBACK_VISION_PRESET,
        screen_max_width_px=width if isinstance(width, int) and not isinstance(width, bool)
        and width > 0 else DEFAULT_SCREEN_MAX_WIDTH_PX,
        workers=_workers(tools),
        open_path=_block(tools, "open_path"),
    )


def _remind_before_min(raw: Mapping[str, Any]) -> int:
    """``commitments.remind_before_min``, how long before it a thing he goes to rings; else 60."""
    before = _block(raw, "commitments").get("remind_before_min")
    if isinstance(before, int | float) and not isinstance(before, bool) and (
        1 <= before <= _MINUTES_IN_A_DAY
    ):
        return int(before)
    return _FALLBACK_REMIND_BEFORE_MIN


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
        ambient_sounds=_switch(realtime, "ambient_sounds", where="realtime"),
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


def _bar(value: object) -> float | None:
    """A confidence bar in (0, 1], or None when it is anything else."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 < value <= 1:
        return None
    return float(value)


def _surrogate_route(raw: Mapping[str, Any], config_path: Path) -> SurrogateRouteSettings | None:
    """``realtime.surrogate_route`` (ADR 0122): off unless enabled; bad values stop the load."""
    realtime = raw.get("realtime")
    block = realtime.get("surrogate_route") if isinstance(realtime, Mapping) else None
    if not isinstance(block, Mapping) or block.get("enabled") is not True:
        return None
    model, bar, timeout = block.get("model"), block.get("min_confidence"), block.get("timeout_ms")
    if (
        not isinstance(model, str) or not model.strip()
        or isinstance(bar, bool) or not isinstance(bar, int | float) or not 0 < bar <= 1
        or isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0
    ):
        msg = (
            f"runtime: {config_path} realtime.surrogate_route needs model (text),"
            " min_confidence (0-1] and timeout_ms (positive int)"
        )
        raise RuntimeBootstrapError(msg)
    return SurrogateRouteSettings(
        model=model.strip(), min_confidence=float(bar), timeout_ms=timeout,
        parallel=block.get("parallel") is True, zdr=block.get("zdr") is not False,
    )


def _turn_end_asks(raw: Mapping[str, Any], config_path: Path) -> TurnEndAsksSettings | None:
    """``agents.turn_end_asks`` (ADR 0125): off unless enabled; bad values stop the load."""
    agents = raw.get("agents")
    block = agents.get("turn_end_asks") if isinstance(agents, Mapping) else None
    if not isinstance(block, Mapping) or block.get("enabled") is not True:
        return None
    model, bar, timeout = block.get("model"), block.get("at"), block.get("timeout_ms")
    question_bar = block.get("question_at")
    if (
        not isinstance(model, str) or not model.strip()
        or isinstance(bar, bool) or not isinstance(bar, int | float) or not 0 < bar <= 1
        or isinstance(question_bar, bool) or not isinstance(question_bar, int | float)
        or not 0 < question_bar <= bar
        or isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0
    ):
        msg = (
            f"runtime: {config_path} agents.turn_end_asks needs model (text), at in (0, 1],"
            " question_at in (0, at] and timeout_ms (positive int)"
        )
        raise RuntimeBootstrapError(msg)
    return TurnEndAsksSettings(
        model=model.strip(), at=float(bar), question_at=float(question_bar), timeout_ms=timeout,
    )


def _voice_words(raw: Mapping[str, Any], config_path: Path) -> VoiceWordsSettings | None:
    """``realtime.jev_words`` (ADR 0130): off unless enabled; bad values stop the load."""
    realtime = raw.get("realtime")
    block = realtime.get("jev_words") if isinstance(realtime, Mapping) else None
    if not isinstance(block, Mapping) or block.get("enabled") is not True:
        return None
    model, bar = block.get("model"), block.get("at")
    timeout, chars = block.get("timeout_ms"), block.get("max_chars")
    if (
        not isinstance(model, str) or not model.strip()
        or isinstance(bar, bool) or not isinstance(bar, int | float) or not 0 < bar <= 1
        or isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0
        or isinstance(chars, bool) or not isinstance(chars, int) or chars <= 0
    ):
        msg = (
            f"runtime: {config_path} realtime.jev_words needs model (text), at in (0, 1],"
            " timeout_ms (positive int) and max_chars (positive int)"
        )
        raise RuntimeBootstrapError(msg)
    return VoiceWordsSettings(
        model=model.strip(), at=float(bar), timeout_ms=timeout, max_chars=chars,
    )


def _jev_oneshot(raw: Mapping[str, Any], config_path: Path) -> JevOneShotSettings | None:
    """``realtime.jev_oneshot`` (ADR 0139, 0140): off unless enabled; bad values stop the load.

    It needs ``realtime.surrogate_route`` on (its instant functions and transport bar); the
    control words need ``realtime.jev_words`` too, and without it the surface asks nothing.
    """
    realtime = raw.get("realtime")
    if not isinstance(realtime, Mapping):
        return None
    block = realtime.get("jev_oneshot")
    if not isinstance(block, Mapping) or block.get("enabled") is not True:
        return None
    prefix = f"runtime: {config_path} realtime.jev_oneshot"
    model, timeout = block.get("model"), block.get("timeout_ms")
    relation, tool_line = block.get("relation"), block.get("tool_line")
    words = realtime.get("jev_words")
    surrogate = realtime.get("surrogate_route")
    if not isinstance(surrogate, Mapping) or surrogate.get("enabled") is not True:
        msg = f"{prefix} needs realtime.surrogate_route enabled"
        raise RuntimeBootstrapError(msg)
    if (
        not isinstance(model, str) or not model.strip()
        or isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0
        or not isinstance(relation, Mapping) or not isinstance(tool_line, Mapping)
    ):
        msg = f"{prefix} needs model (text), timeout_ms (positive int), relation and tool_line"
        raise RuntimeBootstrapError(msg)
    relation_at = _bar(relation.get("at")) if relation.get("enabled") is True else None
    window = relation.get("window_s")
    tool_at = _bar(tool_line.get("at")) if tool_line.get("enabled") is True else None
    groups = tool_line.get("groups")
    if (
        (relation.get("enabled") is True and relation_at is None)
        or isinstance(window, bool) or not isinstance(window, int | float) or window <= 0
        or (tool_line.get("enabled") is True and tool_at is None)
        or not isinstance(groups, list) or not all(g in TOOL_GROUPS and g != "none" for g in groups)
    ):
        msg = (
            f"{prefix}.relation needs at in (0, 1] and window_s > 0; .tool_line needs at in"
            " (0, 1] and groups from the tool group names"
        )
        raise RuntimeBootstrapError(msg)
    words_at = words_timeout = words_chars = None
    if isinstance(words, Mapping) and words.get("enabled") is True:
        words_at, words_timeout = _bar(words.get("at")), words.get("timeout_ms")
        words_chars = words.get("max_chars")
        if (
            words_at is None
            or isinstance(words_timeout, bool) or not isinstance(words_timeout, int)
            or isinstance(words_chars, bool) or not isinstance(words_chars, int)
        ):
            msg = f"{prefix} reads realtime.jev_words at, timeout_ms and max_chars: not valid"
            raise RuntimeBootstrapError(msg)
    return JevOneShotSettings(
        model=model.strip(), timeout_ms=timeout, zdr=block.get("zdr") is not False,
        relation_at=relation_at, window_s=float(window), tool_at=tool_at,
        tool_groups=frozenset(groups), words_at=words_at,
        words_timeout_ms=words_timeout or 800, words_max_chars=words_chars or 24,
    )


def _jev(raw: Mapping[str, Any], config_path: Path) -> JevSettings:
    """Jev's routes: ``jev_log`` (ADR 0128) is on unless ``enabled: false``, the rest off."""
    log = raw.get("jev_log")
    return JevSettings(
        log=not (isinstance(log, Mapping) and log.get("enabled") is False),
        surrogate_route=_surrogate_route(raw, config_path),
        turn_end_asks=_turn_end_asks(raw, config_path),
        voice_words=_voice_words(raw, config_path),
        oneshot=_jev_oneshot(raw, config_path),
    )


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
    work_state = _work_state(raw, config_path)
    home_weather = _home_weather(raw)
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
        work_state=work_state,
        daily_report=_daily_report(raw, work_state),
        locate=_locate(raw),
        home_weather=home_weather,
        transit_places=_transit_places(raw, home_weather),
        maps_monthly_usd=_maps_monthly_usd(raw),
        dashboard=_dashboard(raw),
        diagnostics=_diagnostics(raw),
        tools=_tools(raw),
        voiceprint_gate=_switch(_block(raw, "voiceprint"), "gate", where="voiceprint"),
        remind_before_min=_remind_before_min(raw),
        plan_day_ends=day_ends_of(raw.get("day_plan")),
        jev=_jev(raw, config_path),
        raw=raw,
    )
