"""Immutable per-ResponseRun LLM request clients (ADR-0008 D4 / F19).

The shared :class:`~jarvis.decision.llm.LLMClient` carries mutable provider
identity: ``switch_model``/``_apply_preset`` rebind provider, model, base URL
and key in place, and ``_last_metadata`` is reset wholesale on every
``chat()`` entry.  With one client per process that is invisible, because at
most one turn is ever in flight.  The moment two ResponseRuns overlap it
becomes a cost-attribution bug: ``CostRecorder._from_client`` and the legacy
``_emit_cost_recorded_from_metadata`` path both read those mutable
``last_*`` accessors, so the second run's request id can be stamped onto the
first run's ``cost.recorded`` row.

:class:`LLMSessionFactory` removes the shared identity rather than guarding
it.  Each run gets its own :class:`LLMRequestClient` built from a frozen
:class:`LLMPresetSnapshot`, so per-instance state is correct by construction
and no accounting code changes at all.

``LLMRequestClient`` subclasses ``LLMClient`` deliberately.
``DecideContext.llm_client``, ``CostRecorder.chat``/``chat_stream`` and
``reviewer.review_diff`` are all annotated against ``LLMClient``; subclassing
keeps every one of those annotations — and therefore every legacy code path —
untouched, which is what makes "flags off is byte-for-byte legacy" cheap to
prove.  A wrapper type would force a second ``DecideContext`` field and a
branch inside the cost guard for no verification gain.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping  # runtime use: isinstance in the preset reader.
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from jarvis.decision.llm import LLMClient

if TYPE_CHECKING:
    from jarvis.decision.llm import Provider

_SYNTHETIC_PRESET_NAME = "__request__"


class ImmutableRequestClientError(RuntimeError):
    """A per-run request client refused a mutation of its pinned preset."""


class UnknownRequestPresetError(KeyError):
    """The factory was asked for a preset name that is not configured."""


@dataclass(frozen=True)
class LLMPresetSnapshot:
    """Immutable provider/model/preset identity for exactly one ResponseRun."""

    preset_name: str | None
    provider: Provider
    model: str
    base_url: str
    max_tokens: int
    api_key_env: str | None
    timeout_s: float | None
    max_retries: int | None
    snapshot_hash: str


def _snapshot_hash(fields: Mapping[str, object]) -> str:
    """Return a stable sha256 over the snapshot's canonical JSON form."""
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class LLMSessionFactory:
    """Mint one immutable :class:`LLMRequestClient` per ResponseRun.

    Construction reads nothing from the environment and opens no provider
    transport; it only takes a deep defensive copy of the parsed ``llm:``
    mapping.  The copy must be deep because presets are nested dicts — a
    shallow copy would still alias them, and a later config mutation would
    silently repoint a live run's model.
    """

    def __init__(self, llm_config: Mapping[str, Any]) -> None:
        """Store a deep copy of the parsed ``llm:`` configuration block."""
        self._config: dict[str, Any] = copy.deepcopy(dict(llm_config))

    def snapshot(self, preset_name: str | None = None) -> LLMPresetSnapshot:
        """Freeze one preset's provider identity.

        ``preset_name=None`` resolves ``llm_config["default_preset"]`` — the
        preset ``LLMClient(llm_config)`` would apply at construction — so a
        run started with no explicit preset is identical to the legacy shared
        client's active configuration.

        Raises:
            UnknownRequestPresetError: ``preset_name`` is not configured.
        """
        presets_raw = self._config.get("presets") or {}
        presets: Mapping[str, Any] = presets_raw if isinstance(presets_raw, Mapping) else {}

        resolved = preset_name
        if resolved is None:
            default = self._config.get("default_preset")
            resolved = str(default) if default and str(default) in presets else None

        provider_raw = str(self._config.get("provider", "openai")).strip().lower()
        model = str(self._config.get("model", ""))
        base_url = str(self._config.get("base_url", "") or "")
        max_tokens = int(self._config.get("max_tokens", 0) or 0)
        api_key_env_raw = self._config.get("api_key_env")
        api_key_env = str(api_key_env_raw) if api_key_env_raw else None

        if resolved is not None:
            preset = presets.get(resolved)
            if not isinstance(preset, Mapping):
                msg = f"preset {resolved!r} is not configured under llm.presets"
                raise UnknownRequestPresetError(msg)
            preset_provider = preset.get("provider")
            if preset_provider:
                provider_raw = str(preset_provider).strip().lower()
            model = str(preset.get("model", model))
            base_url = str(preset.get("base_url", "") or "")
            if "max_tokens" in preset:
                max_tokens = int(preset["max_tokens"])
            preset_key_env = preset.get("api_key_env")
            api_key_env = str(preset_key_env) if preset_key_env else None
        elif preset_name is not None:
            msg = f"preset {preset_name!r} is not configured under llm.presets"
            raise UnknownRequestPresetError(msg)

        if provider_raw not in ("openai", "anthropic"):
            msg = f"unsupported provider {provider_raw!r}; expected 'openai' or 'anthropic'"
            raise ValueError(msg)
        provider: Provider = "openai" if provider_raw == "openai" else "anthropic"
        if resolved is None and api_key_env is None:
            # Synthetic presets must retain the legacy flat-config fallback.
            api_key_env = "OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY"

        raw_timeout = self._config.get("timeout_s")
        timeout_s = (
            float(raw_timeout)
            if isinstance(raw_timeout, (int, float)) and not isinstance(raw_timeout, bool)
            else None
        )
        raw_max_retries = self._config.get("max_retries")
        max_retries = (
            int(raw_max_retries)
            if isinstance(raw_max_retries, int) and not isinstance(raw_max_retries, bool)
            else None
        )

        fields: dict[str, object] = {
            "preset_name": resolved,
            "provider": provider,
            "model": model,
            "base_url": base_url,
            "max_tokens": max_tokens,
            "api_key_env": api_key_env,
            "timeout_s": timeout_s,
            "max_retries": max_retries,
        }
        return LLMPresetSnapshot(
            preset_name=resolved,
            provider=provider,
            model=model,
            base_url=base_url,
            max_tokens=max_tokens,
            api_key_env=api_key_env,
            timeout_s=timeout_s,
            max_retries=max_retries,
            snapshot_hash=_snapshot_hash(fields),
        )

    def create(
        self,
        preset_snapshot: LLMPresetSnapshot,
        *,
        response_id: str,
        tracker: object | None = None,
    ) -> LLMRequestClient:
        """Build one request client pinned to ``preset_snapshot``."""
        return LLMRequestClient(
            snapshot=preset_snapshot,
            response_id=response_id,
            tracker=tracker,
        )


class LLMRequestClient(LLMClient):
    """One immutable provider client bound to exactly one ResponseRun."""

    def __init__(
        self,
        *,
        snapshot: LLMPresetSnapshot,
        response_id: str,
        tracker: object | None = None,
    ) -> None:
        """Construct from a frozen snapshot; the preset is applied once."""
        preset_name = snapshot.preset_name or _SYNTHETIC_PRESET_NAME
        preset: dict[str, Any] = {
            "provider": snapshot.provider,
            "model": snapshot.model,
            "base_url": snapshot.base_url,
            "max_tokens": snapshot.max_tokens,
        }
        if snapshot.api_key_env is not None:
            preset["api_key_env"] = snapshot.api_key_env
        request_config: dict[str, Any] = {
            "provider": snapshot.provider,
            "presets": {preset_name: preset},
            "default_preset": preset_name,
        }
        if snapshot.timeout_s is not None:
            request_config["timeout_s"] = snapshot.timeout_s
        if snapshot.max_retries is not None:
            request_config["max_retries"] = snapshot.max_retries
        super().__init__(request_config, tracker)
        self._preset_snapshot = snapshot
        self._response_id = response_id

    @property
    def preset_snapshot(self) -> LLMPresetSnapshot:
        """Return the frozen preset identity this client is pinned to."""
        return self._preset_snapshot

    @property
    def response_id(self) -> str:
        """Return the ResponseRun id that owns this client."""
        return self._response_id

    def switch_model(self, preset_name: str) -> str:
        """Refuse every preset change (ADR-0008 D4).

        Raises:
            ImmutableRequestClientError: Always. Provider identity belongs to
                the ResponseRun; a new identity requires a new run.
        """
        msg = (
            f"LLMRequestClient for response_id={self._response_id!r} is pinned to "
            f"preset {self._preset_snapshot.preset_name!r}; refusing to switch to "
            f"{preset_name!r}"
        )
        raise ImmutableRequestClientError(msg)


def snapshot_fields(snapshot: LLMPresetSnapshot) -> Mapping[str, object]:
    """Return ``snapshot`` as a plain mapping (diagnostics and burn output)."""
    return asdict(snapshot)


__all__ = [
    "ImmutableRequestClientError",
    "LLMPresetSnapshot",
    "LLMRequestClient",
    "LLMSessionFactory",
    "UnknownRequestPresetError",
    "snapshot_fields",
]
