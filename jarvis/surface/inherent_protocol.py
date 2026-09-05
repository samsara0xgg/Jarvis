"""Inherent realtime v2 wire DTOs — ADR-0014 §5 D6/D7.

L5 surface module, and deliberately the thinnest one in the layer: it owns
the *shape* of every frame the v2 socket exchanges and nothing else.  No
identity is minted here, no cursor is read, no capability is discovered —
those are facts the runtime injects into the server as callables.  Keeping
the DTOs free of that lets the Swift client's Codable mirrors and these
models be checked against the same golden fixtures.

Layer rules (L5): stdlib and ``pydantic`` only.  This module names no
``jarvis`` package at all.

Decoding is strict on purpose.  A realtime protocol that coerces ``"2"``
into ``2`` or silently accepts a negative cursor cannot fail closed later:
the damage shows up as a client applying deltas in the wrong order.  The
one place laxity is required is forward compatibility — unknown fields are
ignored (D6), so a newer peer can add keys without breaking an older one.
"""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)

PROTOCOL_VERSION: Final[int] = 2
VIEW_SCHEMA_VERSION: Final[int] = 1
MAX_CLIENT_FRAME_BYTES: Final[int] = 64 * 1024
REQUIRED_CLIENT_CAPABILITIES: Final[tuple[str, ...]] = ("paged_snapshot", "transport_ack")
HELLO_TIMEOUT_S: Final[float] = 2.0
INITIAL_MAX_FRAMES_PER_S: Final[int] = 50

_BASE_CONFIG = ConfigDict(strict=True, extra="ignore", frozen=True)

Identity = Annotated[StrictStr, Field(min_length=1, max_length=128)]
Cursor = Annotated[StrictInt, Field(ge=0)]
EpochMs = Annotated[StrictInt, Field(ge=0)]
ShortText = Annotated[StrictStr, Field(max_length=256)]
DeliveryClass = Literal["durable", "ephemeral", "protocol"]
ResumeMode = Literal["snapshot", "incremental"]

_HELLO_MESSAGE_TYPE: Final[str] = "client.hello"


class ClientEnvelope(BaseModel):
    """Any frame the client sends over the v2 socket (D6).

    ``protocol_version`` is a plain positive integer rather than
    ``Literal[2]``: a client speaking an unsupported version must still
    decode far enough for hello to answer ``upgrade_required``, which is a
    much better failure than an opaque ``protocol_error``.
    """

    model_config = _BASE_CONFIG

    protocol_version: Annotated[StrictInt, Field(ge=1)]
    message_type: Identity
    message_id: Identity
    client_instance_id: Identity
    connection_id: Identity | None
    sent_at_ms: EpochMs
    payload: dict[str, Any]

    @model_validator(mode="after")
    def _check_connection_id_matches_message_type(self) -> Self:
        """Only the hello may omit ``connection_id``, and it must omit it."""
        is_hello = self.message_type == _HELLO_MESSAGE_TYPE
        if is_hello and self.connection_id is not None:
            message = "client.hello must not carry a connection_id"
            raise ValueError(message)
        if not is_hello and self.connection_id is None:
            message = "a post-hello client frame must carry a connection_id"
            raise ValueError(message)
        return self


class ClientHelloPayload(BaseModel):
    """The D7 hello payload: what the client is and what it already holds."""

    model_config = _BASE_CONFIG

    supported_versions: Annotated[list[Annotated[StrictInt, Field(ge=1)]], Field(min_length=1)]
    client_build: ShortText
    view_schema_versions: Annotated[list[Annotated[StrictInt, Field(ge=1)]], Field(min_length=1)]
    capabilities: list[ShortText]
    last_log_epoch: Identity | None
    last_applied_cursor: Cursor | None
    has_complete_local_state: StrictBool


class ClientHello(ClientEnvelope):
    """The first frame on every accepted v2 socket (D7)."""

    message_type: Literal["client.hello"]
    # Narrowing an untyped payload to its typed shape is what the hello
    # subclasses exist for; the models are frozen, so the substitution
    # mypy objects to cannot actually be observed through a write.
    payload: ClientHelloPayload  # type: ignore[assignment]


class ServerEnvelope(BaseModel):
    """Any frame the daemon sends over the v2 socket (D6).

    The cursor rules are the reason this type exists: durable deltas are
    ordered and ACKed by ``event_cursor``, ephemeral updates by a
    connection-scoped ``ephemeral_sequence``, and mixing the two would let
    a client ACK a cursor it never received.
    """

    model_config = _BASE_CONFIG

    protocol_version: Literal[2]
    message_type: Identity
    message_id: Identity
    delivery_class: DeliveryClass
    connection_id: Identity
    log_epoch: Identity
    boot_id: Identity
    event_cursor: Cursor | None = None
    ephemeral_sequence: Cursor | None = None
    sent_at_ms: EpochMs
    payload: dict[str, Any]

    @model_validator(mode="after")
    def _check_cursors_match_delivery_class(self) -> Self:
        """Each delivery class carries exactly the ordering key it owns."""
        if self.delivery_class == "durable":
            if self.event_cursor is None or self.ephemeral_sequence is not None:
                message = "a durable frame carries event_cursor and no ephemeral_sequence"
                raise ValueError(message)
        elif self.delivery_class == "ephemeral":
            if self.ephemeral_sequence is None or self.event_cursor is not None:
                message = "an ephemeral frame carries ephemeral_sequence and no event_cursor"
                raise ValueError(message)
        elif self.event_cursor is not None or self.ephemeral_sequence is not None:
            message = "a protocol frame carries neither cursor"
            raise ValueError(message)
        return self


class RuntimeCapabilities(BaseModel):
    """What this daemon can actually do right now, per D7.

    Computed from live routes rather than declared: ``image_input`` stays
    false while the image endpoint is a 501 stub.
    """

    model_config = _BASE_CONFIG

    text_input: StrictBool
    image_input: StrictBool
    voice_input: StrictBool
    response_interrupt: StrictBool
    action_cancel: StrictBool
    confirmation_actions: StrictBool
    natural_barge_in: StrictBool
    aec_profile: ShortText


class ServerHelloPayload(BaseModel):
    """The D7 server answer: version, resume decision, and capabilities."""

    model_config = _BASE_CONFIG

    selected_version: Literal[2]
    view_schema_version: Literal[1]
    resume_mode: ResumeMode
    server_high_water_cursor: Cursor
    required_client_capabilities: list[ShortText]
    runtime_capabilities: RuntimeCapabilities


class ServerHello(ServerEnvelope):
    """The daemon's reply to a supported ``client.hello`` (D7)."""

    message_type: Literal["server.hello"]
    delivery_class: Literal["protocol"]
    payload: ServerHelloPayload  # type: ignore[assignment]


# --- Durable deltas, snapshot frames and transport ACK (D8, D10, D11) --------

Sha256Hex = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
ResponsePhase = Literal["commentary", "final"]
ResponseChannel = Literal["speech", "document", "both"]
ResponseLifecycle = Literal[
    "generating", "waiting_action", "finalizing", "completed", "cancelled", "failed"
]
PanelStreamState = Literal["unopened", "open", "closed", "failed"]


class ResponseOpened(BaseModel):
    """D10 ``response.opened``: a response and, on first sight, its group."""

    model_config = _BASE_CONFIG

    kind: Literal["response.opened"]
    response_id: Identity
    response_group_id: Identity
    turn_id: ShortText
    phase: ResponsePhase
    channel: ResponseChannel
    lifecycle: ResponseLifecycle
    question: StrictStr | None
    summary: StrictStr | None
    created_at_ms: EpochMs
    revision: Cursor


class ResponseSegment(BaseModel):
    """D10 ``response.segment``: one permitted panel segment.

    ``truncated`` appears only inside a snapshot preview whose first segment
    alone exceeded the inline budget; a live segment never carries it.
    """

    model_config = _BASE_CONFIG

    kind: Literal["response.segment"]
    response_id: Identity
    sequence: Cursor
    phase: ResponsePhase
    channel: ResponseChannel
    text: StrictStr
    segment_hash: Sha256Hex
    truncated: StrictBool = False


class ResponseDelivery(BaseModel):
    """D10 ``response.delivery``: the panel stream state of one response."""

    model_config = _BASE_CONFIG

    kind: Literal["response.delivery"]
    response_id: Identity
    panel_stream: PanelStreamState
    reason: StrictStr | None


ViewMutation = Annotated[
    ResponseOpened | ResponseSegment | ResponseDelivery,
    Field(discriminator="kind"),
]


class ViewDeltaPayload(BaseModel):
    """The payload of one durable ``view.delta``: one ordered mutation batch (D6/D9)."""

    model_config = _BASE_CONFIG

    source_event_uid: Identity
    changes: list[ViewMutation]


class DocumentReferenceItem(BaseModel):
    """D16 reference to a body the snapshot did not inline."""

    model_config = _BASE_CONFIG

    response_id: Identity
    event_uid: Identity
    utf8_bytes: Cursor


class ResponseSnapshotItem(BaseModel):
    """One response inside a ``response_groups`` snapshot item."""

    model_config = _BASE_CONFIG

    response_id: Identity
    phase: ResponsePhase
    channel: ResponseChannel
    lifecycle: ResponseLifecycle
    revision: Cursor
    panel_stream: PanelStreamState
    segments: list[ResponseSegment]
    playbacks: list[dict[str, Any]]
    document_reference: DocumentReferenceItem | None = None


class ResponseGroupSnapshotItem(BaseModel):
    """One item of the ``response_groups`` section (D8)."""

    model_config = _BASE_CONFIG

    response_group_id: Identity
    turn_id: ShortText
    question: StrictStr | None
    created_at_ms: EpochMs
    responses: list[ResponseSnapshotItem]


class SnapshotBeginPayload(BaseModel):
    """D8 ``snapshot.begin``; ``counts`` is pages per section, keyed as in ``section_order``."""

    model_config = _BASE_CONFIG

    snapshot_id: Identity
    through_cursor: Cursor
    view_schema_version: Literal[1]
    section_order: list[ShortText]
    counts: dict[str, Cursor]


class SnapshotPagePayload(BaseModel):
    """D8 ``snapshot.page``; ``items`` decode by ``section``."""

    model_config = _BASE_CONFIG

    snapshot_id: Identity
    section: ShortText
    page_index: Cursor
    items: list[dict[str, Any]]


class SnapshotEndPayload(BaseModel):
    """D8 ``snapshot.end``; ``content_hash`` covers the exact page frame bytes."""

    model_config = _BASE_CONFIG

    snapshot_id: Identity
    through_cursor: Cursor
    content_hash: Sha256Hex


class TransportAckPayload(BaseModel):
    """D11 rule 6 cumulative ACK; ``snapshot_id`` is set only for a snapshot ACK."""

    model_config = _BASE_CONFIG

    snapshot_id: Identity | None = None
    through_cursor: Cursor


def hello_is_supported(hello: ClientHello) -> bool:
    """Report whether this client can be served, or needs ``upgrade_required``.

    Both directions have to agree: the client must be speaking v2 *and*
    list v2 among the versions it accepts back, must accept view schema 1,
    and must be able to do the two things the server relies on — paged
    snapshots and transport ACKs.

    Args:
        hello: A decoded client hello.

    Returns:
        True when the handshake may proceed to ``server.hello``.
    """
    payload = hello.payload
    return (
        hello.protocol_version == PROTOCOL_VERSION
        and PROTOCOL_VERSION in payload.supported_versions
        and VIEW_SCHEMA_VERSION in payload.view_schema_versions
        and all(capability in payload.capabilities for capability in REQUIRED_CLIENT_CAPABILITIES)
    )
