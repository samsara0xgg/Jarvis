"""The vision seam of ``screen_look`` and the picture notes (ADR 0011 D5/D7)."""

from __future__ import annotations

import base64
import contextlib
import logging
import mimetypes
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from jarvis.decision.cost_guard import CostRecorder
from jarvis.decision.llm import LLMClient
from jarvis.decision.llm_session import LLMSessionFactory
from jarvis.shared.lang import language_name
from jarvis.shared.pricing import load_pricing_table
from jarvis.shared.realtime import new_response_id
from jarvis.state.event_log import open_runtime_event_log

if TYPE_CHECKING:

    from pathlib import Path

    from jarvis.execution.tools import (
        VisionClient,
    )

LOGGER = logging.getLogger(__name__)


# System prompt for the injected vision client (`_LLMVisionClient` below).
# Asking for the user's language means `screen_look`'s text observation
# slots straight into a Tier 0 reply without a translation hop.
_VISION_SYSTEM_PROMPT: str = (
    "You are a screen-reading assistant. Describe what is currently "
    "visible in the screenshot factually and concisely. Respond in {language}."
)


_VISION_CALL_TIMEOUT_S: Final[float] = 20.0
"""Bound on the vision preset's OpenAI SDK call (MUST-FIX 2, ADR-0011
§12). Same 20 s order as `DEFAULT_WEB_TIMEOUT_S` (Step 6's web tools) —
every other seam in `screen_look` is bounded (`_SCREEN_CAPTURE_TIMEOUT_S`
/ `_SIPS_TIMEOUT_S` = 10 s each), yet without this the one network call
had NO timeout: the SDK's own default is `Timeout(connect=5, read=600,
write=600, pool=600)` with `max_retries=2`, i.e. up to ~1800 s blocking
the synchronous `decide()` loop on a hung proxy. Scoped to the vision
client; the decision loop's own bound is `llm.timeout_s` in
config/jarvis.yaml."""


_VISION_CALL_MAX_RETRIES: Final[int] = 1
"""Paired with `_VISION_CALL_TIMEOUT_S` so the worst case is a small
multiple of the timeout (~40 s: one attempt + one retry) rather than
half an hour."""


_VISION_ERROR_EXCERPT_MAX_CHARS: Final[int] = 300
"""Bound on the redacted excerpt `VisionCallError` carries (MUST-FIX 1a,
ADR-0011 §12) — independent of, and a first line of defense ahead of,
the tool dispatcher's own cap in `jarvis.execution.tools`."""


_BASE64_DATA_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"data:[\w./+-]*;base64,[A-Za-z0-9+/=]+",
)


_LONG_BASE64_RUN_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9+/]{64,}={0,2}")


class VisionCallError(RuntimeError):
    """Raised by `_LLMVisionClient.describe_image` in place of the raw SDK error.

    MUST-FIX 1a, ADR-0011 §12: this is the ONE call site that holds the
    screenshot's base64 data URL, so an upstream (proxy or provider)
    that echoes the request body it failed on — a common thin-proxy
    error shape — would otherwise ship image bytes into `str(exc)`,
    which `screen_look`'s handler interpolates verbatim into
    `action.result_observed` (durable SQLite, spec §3.3.9 forbids this)
    and into the tool-result message the decision LLM sees. Redaction
    is by PATTERN (a `data:...;base64,` URL, or any standalone long
    base64-alphabet run), not by trusting the upstream's error shape —
    the whole point is that an arbitrary upstream can echo the request
    in a format this code has never seen.
    """


def _redact_vision_error(exc: Exception) -> str:
    """Scrub base64 image payloads out of a vision-call exception's message.

    Returns `"{type name}: {bounded, redacted excerpt}"` — diagnosable
    (the real exception type survives) without risking a fresh
    un-redacted echo downstream. `raise ... from None` at the call site
    severs the exception chain so nothing that later prints a full
    traceback (e.g. `__cause__`/`__context__`) can resurrect the
    original, un-redacted message either.
    """
    text = str(exc)
    text = _BASE64_DATA_URL_RE.sub("data:[redacted-base64]", text)
    text = _LONG_BASE64_RUN_RE.sub("[redacted-base64]", text)
    if len(text) > _VISION_ERROR_EXCERPT_MAX_CHARS:
        text = text[:_VISION_ERROR_EXCERPT_MAX_CHARS] + "…[truncated]"
    return f"{type(exc).__name__}: {text}"


class _LLMVisionClient:
    """Adapts `LLMClient` (L3) to `jarvis.execution.tools.VisionClient` (ADR-0011 D5/D7).

    L4 cannot import L3 (`.importlinter` sibling isolation), so
    `screen_look`'s one vision call is injected through this
    composition-root-only adapter. Reads the image bytes and
    base64-encodes them HERE, at call time, inside the vision request
    only — never in the event-log payload (spec §3.3.9 / ADR-0011 §3
    D5): the L4 handler only ever hands this a `Path` and gets a `str`
    back.
    """

    def __init__(
        self,
        llm_client: LLMClient,
        *,
        cost_recorder: CostRecorder,
    ) -> None:
        """Wrap a vision-preset-bound `LLMClient` (see `_build_vision_client`)."""
        self._llm_client = llm_client
        self._cost_recorder = cost_recorder

    def describe_image(
        self, image_path: Path, *, question: str | None, instructions: str | None = None,
    ) -> str:
        """Send one image + optional question to the vision preset; return text.

        ``instructions`` replaces the screen-reading system prompt (a picture he sent, ADR 0223).
        Raises `VisionCallError` — never the raw SDK/HTTP exception —
        on any failure (MUST-FIX 1a, ADR-0011 §12): see that class's
        docstring for why.
        """
        mime = mimetypes.guess_type(image_path.name)[0] or "image/png"
        data_url = f"data:{mime};base64," + base64.b64encode(
            image_path.read_bytes(),
        ).decode("ascii")
        prompt_text = question or "Describe what is currently on this screen."
        system = instructions or _VISION_SYSTEM_PROMPT.format(language=language_name())
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_text},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ]
        try:
            result = self._cost_recorder.chat(
                self._llm_client,
                messages=messages,
                system=system,
                kind="vision",
                turn_id=None,
            )
        except Exception as exc:  # noqa: BLE001 — deliberately re-raised, scrubbed, as VisionCallError; see MUST-FIX 1a.
            raise VisionCallError(_redact_vision_error(exc)) from None
        return result.text or ""


class _RequestScopedVisionClient:
    """Mint isolated provider and accounting state in the calling worker."""

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        event_log_path: Path,
        pricing_path: Path | None,
    ) -> None:
        self._factory = LLMSessionFactory(config)
        self._snapshot = self._factory.snapshot()
        self._event_log_path = event_log_path
        self._pricing = {} if pricing_path is None else load_pricing_table(pricing_path)

    def describe_image(
        self, image_path: Path, *, question: str | None, instructions: str | None = None,
    ) -> str:
        """Perform one bounded request with its own SQLite connection."""
        client = self._factory.create(self._snapshot, response_id=new_response_id())
        with contextlib.closing(open_runtime_event_log(self._event_log_path)) as conn:
            recorder = CostRecorder(conn, pricing_table=self._pricing)
            return _LLMVisionClient(client, cost_recorder=recorder).describe_image(
                image_path, question=question, instructions=instructions,
            )


def _build_vision_client(
    config: Mapping[str, Any],
    preset_name: str,
    *,
    event_log_path: Path,
    pricing_path: Path | None = None,
) -> VisionClient | None:
    """Build the `screen_look` vision seam from `llm.presets.<preset_name>` (ADR-0011 D7).

    Returns `None` when the preset is absent/malformed — either
    STRUCTURALLY (missing `llm`/`presets` block, preset not a mapping)
    or by CONTENT (an unsupported `provider` string, a non-numeric
    `max_tokens` — SHOULD-FIX 3, ADR-0011 §12): `screen_look` still
    registers (the menu stays complete) but every call degrades to a
    `vision_unconfigured` error observation (ADR-0011 §5). A malformed
    preset is one optional tool misconfigured, not a trust source going
    missing (contrast `_load_file_targets_config`'s deliberate
    fail-boot posture, which protects a trust source the gate
    consults) — so it must not take the whole daemon down at boot.

    A DEDICATED `LLMClient` is built here rather than reusing the
    decision loop's own client (constructed at step 4, below): a client
    is pinned to one preset, so the vision model is a client of its own.
    This reuses the SAME loader
    (`LLMClient.__init__` / `_apply_preset` reading a `presets` dict) —
    not a parallel YAML parser — just a second instance scoped to one
    preset, with an explicit bounded `timeout_s`/`max_retries`
    (MUST-FIX 2) the decision client does not get.
    """
    llm_block = config.get("llm")
    if not isinstance(llm_block, Mapping):
        return None
    presets = llm_block.get("presets")
    if not isinstance(presets, Mapping):
        return None
    preset = presets.get(preset_name)
    if not isinstance(preset, Mapping):
        return None
    vision_llm_config: dict[str, Any] = {
        "provider": "openai",
        "presets": {preset_name: dict(preset)},
        "default_preset": preset_name,
        "timeout_s": _VISION_CALL_TIMEOUT_S,
        "max_retries": _VISION_CALL_MAX_RETRIES,
    }
    try:
        LLMClient(vision_llm_config)
    except (ValueError, TypeError) as exc:
        LOGGER.warning(
            "screen_look: llm.presets.%s is malformed (%s: %s); screen_look will "
            "report vision_unconfigured at use time instead of the daemon failing to boot",
            preset_name,
            type(exc).__name__,
            exc,
        )
        return None
    return _RequestScopedVisionClient(
        vision_llm_config,
        event_log_path=event_log_path,
        pricing_path=pricing_path,
    )
