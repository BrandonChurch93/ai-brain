"""The models must agree with the schema, which outranks them.

A discriminated union needs static literals, so the literal sets in
`wire.models` are the one place model content overlaps schema content. Every
one of them is asserted here. If the schema grows a capability class or a
status and the models do not follow, this fails.
"""

from __future__ import annotations

from typing import Any, Literal, get_args

import pytest

from wire import ENVELOPE_BY_TYPE, message_types, protocol_schema
from wire.models import (
    LATCHED_STATES,
    TERMINAL_STATUSES,
    BodyState,
    BootState,
    CapabilityClass,
    CommandStatus,
    ErrorCode,
    HardwareClass,
    RejectCode,
)


def _schema_enum(*path: str) -> set[str]:
    node: Any = protocol_schema()["$defs"]
    for part in path:
        node = node[part]
    return set(node["enum"])


LITERAL_VS_SCHEMA = [
    (CapabilityClass, ("Capability", "properties", "class")),
    (HardwareClass, ("Manifest", "properties", "hardware_class")),
    (BootState, ("Manifest", "properties", "boot_state")),
    (RejectCode, ("RejectPayload", "properties", "code")),
    (BodyState, ("HeartbeatPayload", "properties", "state")),
    (CommandStatus, ("CommandResultPayload", "properties", "status")),
    (ErrorCode, ("ErrorPayload", "properties", "code")),
]


@pytest.mark.parametrize(
    ("literal", "path"), LITERAL_VS_SCHEMA, ids=[path[0] for _, path in LITERAL_VS_SCHEMA]
)
def test_literal_matches_schema_enum(literal: Any, path: tuple[str, ...]) -> None:
    assert set(get_args(literal)) == _schema_enum(*path)


def test_every_schema_message_type_has_an_envelope_model() -> None:
    assert set(ENVELOPE_BY_TYPE) == message_types()


def test_envelope_discriminants_match_their_registry_key() -> None:
    for type_name, envelope in ENVELOPE_BY_TYPE.items():
        annotation = envelope.model_fields["type"].annotation
        assert get_args(annotation) == (type_name,)


def test_terminal_and_latched_sets_are_drawn_from_the_schema() -> None:
    """SPEC 6.7 and 8.2 name these subsets; both must exist in the schema."""
    assert TERMINAL_STATUSES.issubset(_schema_enum("CommandResultPayload", "properties", "status"))
    assert LATCHED_STATES.issubset(_schema_enum("HeartbeatPayload", "properties", "state"))


def test_command_envelopes_require_span_id() -> None:
    """SPEC 6.6: without span_id a retransmitted command could execute twice."""
    for type_name in ("command", "command_result"):
        assert ENVELOPE_BY_TYPE[type_name].model_fields["span_id"].is_required()


def test_only_hello_and_reject_may_omit_session() -> None:
    """SPEC section 4."""
    optional = {
        type_name
        for type_name, envelope in ENVELOPE_BY_TYPE.items()
        if not envelope.model_fields["session"].is_required()
    }
    assert optional == {"hello", "reject"}


def test_literals_are_literals() -> None:
    """Guards the parametrize above: a non-Literal would make get_args empty
    and every comparison vacuously compare two empty sets."""
    for literal, _ in LITERAL_VS_SCHEMA:
        assert getattr(literal, "__origin__", None) is Literal
        assert get_args(literal)
