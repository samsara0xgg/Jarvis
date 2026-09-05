"""Golden-fixture acceptance for the Inherent v2 envelope DTOs (lane C slice 3).

The fixtures under ``tests/fixtures/inherent_v2/`` are the shared contract:
these tests and the Swift ``RealtimeProtocolTests`` decode the same bytes,
so a drift between the two implementations shows up as a failing test in
whichever half moved rather than as a mismatched frame on a live socket.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from jarvis.surface.inherent_protocol import (
    ClientHello,
    ServerEnvelope,
    ServerHello,
    hello_is_supported,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "inherent_v2"

# The kind recorded in `malformed.json` names the decoder that must reject
# the frame; Swift reads the same strings.
_DECODERS: dict[str, type[BaseModel]] = {
    "client_hello": ClientHello,
    "server": ServerEnvelope,
    "server_hello": ServerHello,
}

_MINIMUM_MALFORMED_CASES = 15


def _load(name: str) -> Any:  # noqa: ANN401 — fixture JSON is deliberately untyped here.
    """Read one fixture file as parsed JSON."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("client.hello.json", ClientHello),
        ("server.hello.json", ServerHello),
        ("server.durable.json", ServerEnvelope),
        ("server.ephemeral.json", ServerEnvelope),
        ("server.protocol.json", ServerEnvelope),
    ],
)
def test_every_fixture_decodes_with_its_model(name: str, model: type[BaseModel]) -> None:
    """Each golden frame is accepted by the DTO that owns its shape."""
    assert model.model_validate(_load(name))


def test_client_hello_carries_the_d7_fields() -> None:
    """The hello fixture is the D7 example, not merely well-formed."""
    hello = ClientHello.model_validate(_load("client.hello.json"))

    assert hello.protocol_version == 2
    assert hello.connection_id is None
    assert hello.payload.supported_versions == [2]
    assert hello.payload.view_schema_versions == [1]
    assert hello.payload.capabilities == [
        "paged_snapshot",
        "transport_ack",
        "response_control",
        "confirmation_control",
    ]
    assert hello.payload.last_log_epoch is None
    assert hello.payload.has_complete_local_state is False


def test_server_hello_carries_the_d7_answer() -> None:
    """The server fixture pins the resume decision and live capabilities."""
    hello = ServerHello.model_validate(_load("server.hello.json"))

    assert hello.delivery_class == "protocol"
    assert hello.event_cursor is None
    assert hello.ephemeral_sequence is None
    assert hello.payload.resume_mode == "snapshot"
    assert hello.payload.server_high_water_cursor == 1840
    assert hello.payload.required_client_capabilities == ["paged_snapshot", "transport_ack"]
    assert hello.payload.runtime_capabilities.image_input is False
    assert hello.payload.runtime_capabilities.aec_profile == "headphones_only"


@pytest.mark.parametrize(
    ("unknown_name", "base_name", "model"),
    [
        ("client.hello.unknown-field.json", "client.hello.json", ClientHello),
        ("server.hello.unknown-field.json", "server.hello.json", ServerHello),
    ],
)
def test_unknown_fields_are_ignored(
    unknown_name: str,
    base_name: str,
    model: type[BaseModel],
) -> None:
    """A newer peer may add keys; D6 says an older peer drops them silently."""
    assert model.model_validate(_load(unknown_name)) == model.model_validate(_load(base_name))


def test_every_malformed_frame_is_rejected() -> None:
    """Each recorded bad frame fails closed under the decoder its kind names."""
    cases = _load("malformed.json")
    assert len(cases) >= _MINIMUM_MALFORMED_CASES, (
        f"malformed.json lists only {len(cases)} cases; the rejection set was trimmed"
    )

    for case in cases:
        model = _DECODERS[case["kind"]]
        with pytest.raises(ValidationError) as excinfo:
            model.model_validate(case["frame"])
        assert excinfo.value.errors()[0]["loc"] == tuple(case["loc"]), case["name"]


def test_hello_support_requires_both_sides_to_agree() -> None:
    """Anything short of full v2 agreement must answer ``upgrade_required``."""
    frame = _load("client.hello.json")
    assert hello_is_supported(ClientHello.model_validate(frame)) is True

    def _rejected(**changes: Any) -> bool:  # noqa: ANN401 — mutates arbitrary fixture fields.
        candidate = json.loads(json.dumps(frame))
        for key, value in changes.items():
            if key in candidate:
                candidate[key] = value
            else:
                candidate["payload"][key] = value
        return hello_is_supported(ClientHello.model_validate(candidate))

    assert _rejected(supported_versions=[3]) is False
    assert _rejected(view_schema_versions=[2]) is False
    assert _rejected(protocol_version=3) is False
    assert _rejected(capabilities=["paged_snapshot", "response_control"]) is False


def test_server_hello_round_trips_through_its_own_json() -> None:
    """What the server emits is what a strict decoder accepts back."""
    hello = ServerHello.model_validate(_load("server.hello.json"))

    assert ServerHello.model_validate_json(hello.model_dump_json()) == hello
