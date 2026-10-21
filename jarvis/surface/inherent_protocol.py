"""Inherent v2 input wire DTOs — ADR-0014 D21.

L5 surface module, and deliberately the thinnest one in the layer: it owns
the *shape* of the two authenticated input requests (``/inherent/submit/v2``
and ``/inherent/asr-submit/v2``) and their receipts, and nothing else.  No
identity is minted here and no cursor is read; the runtime injects those
facts into the server as callables.

Layer rules (L5): stdlib and ``pydantic`` only.  This module names no
``jarvis`` package at all.

Decoding is strict on purpose.  A protocol that coerces ``"2"`` into ``2``
cannot fail closed later.  The one place laxity is required is forward
compatibility: unknown fields are ignored (D6), so a newer peer can add keys
without breaking an older one.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

_BASE_CONFIG = ConfigDict(strict=True, extra="ignore", frozen=True)

Identity = Annotated[StrictStr, Field(min_length=1, max_length=128)]
EpochMs = Annotated[StrictInt, Field(ge=0)]
ShortText = Annotated[StrictStr, Field(max_length=256)]

Sha256Lower = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]


class InputSubmissionFields(BaseModel):
    """The three fields every v2 input request carries (D21).

    ``client_created_at_ms`` is untrusted telemetry: the server never orders
    or identifies anything by it, which is why it is a plain non-negative
    integer with no relation to the Event Log clock.
    """

    model_config = _BASE_CONFIG

    request_id: Identity
    client_instance_id: Identity
    client_created_at_ms: EpochMs


class SubmitV2Request(InputSubmissionFields):
    """Body of ``POST /inherent/submit/v2``: the common fields plus raw text."""

    text: StrictStr


class AsrSubmitV2Request(InputSubmissionFields):
    """The non-file half of ``POST /inherent/asr-submit/v2``'s multipart body.

    ``audio_sha256`` is the client's advertised digest of the uploaded bytes;
    the server recomputes it and refuses a mismatch, so a truncated upload can
    never resolve a receipt against audio nobody recognized.
    """

    audio_sha256: Sha256Lower
    language: ShortText = "zh-CN"


class SubmitV2Response(BaseModel):
    """The D21 accepted receipt: which durable row, and which turn."""

    model_config = _BASE_CONFIG

    status: Literal["accepted"] = "accepted"
    request_id: Identity
    input_event_uid: Identity
    turn_id: ShortText
    session_id: Identity | None = None


class AsrSubmitV2Response(SubmitV2Response):
    """The ASR receipt: the text half rides alongside the same ids."""

    utterance_id: Identity | None = None
    text: StrictStr = ""
    emotion: StrictStr = ""
