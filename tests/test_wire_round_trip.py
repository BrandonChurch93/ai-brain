"""Round trip: model -> JSON -> schema-valid -> model.

Two passes at the same property. The fixtures pin it on the 27 messages we
actually care about; hypothesis pushes it through messages nobody thought to
write down, generated from the schema itself.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis_jsonschema import from_schema

from wire import (
    EventEnvelope,
    EventPayload,
    MalformedFrameError,
    ProtocolValidationError,
    Timestamp,
    decode,
    decode_object,
    encode,
    is_valid,
    message_types,
    protocol_schema,
    to_object,
    without_none,
)

VALID_FIXTURES = sorted((Path(__file__).parent / "fixtures" / "protocol" / "valid").glob("*.json"))


def _generation_variants() -> dict[str, dict[str, Any]]:
    """One flat schema per message type, derived from the Envelope's own
    if/then branches. Nothing here is hand-written: each branch is read out
    of the schema and folded into the envelope it applies to.

    Why not hand the whole schema to the generator: it spends about thirty
    seconds canonicalising the twelve-branch if/then chain, twice over, which
    would dominate the suite. Folding the branches first costs 0.06s. It also
    makes coverage certain rather than probabilistic, since one_of over the
    twelve reaches every type instead of leaving it to the draw.

    Instances still get validated against the real schema by the tests below,
    so a fold that admitted something the real schema does not would fail.
    """
    schema = protocol_schema()
    envelope = schema["$defs"]["Envelope"]
    variants: dict[str, dict[str, Any]] = {}

    for branch in envelope["allOf"]:
        type_name = branch["if"]["properties"]["type"]["const"]
        then = branch["then"]

        properties = dict(envelope["properties"])
        properties["type"] = {"const": type_name}
        properties.update(then.get("properties", {}))

        variants[type_name] = {
            "$defs": schema["$defs"],
            "type": "object",
            "required": sorted(set(envelope["required"]) | set(then.get("required", []))),
            "properties": properties,
        }

    return variants


MESSAGE_STRATEGY = st.one_of(*(from_schema(variant) for variant in _generation_variants().values()))


def test_generation_covers_every_message_type() -> None:
    """Guards the fold: a branch dropped here would silently stop generating
    that type, and the property tests would still pass."""
    assert set(_generation_variants()) == message_types()


@pytest.mark.parametrize("path", VALID_FIXTURES, ids=lambda path: path.stem)
def test_fixture_round_trips_unchanged(path: Path) -> None:
    original = json.loads(path.read_text(encoding="utf-8"))

    model = decode_object(original)
    rendered = to_object(model)

    assert rendered == original, "round trip altered the message"
    assert decode_object(rendered) == model


@pytest.mark.parametrize("path", VALID_FIXTURES, ids=lambda path: path.stem)
def test_fixture_survives_a_string_frame(path: Path) -> None:
    raw = path.read_text(encoding="utf-8")
    model = decode(raw)
    assert decode(encode(model)) == model


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(MESSAGE_STRATEGY)
def test_generated_message_round_trips(raw: Any) -> None:
    """Any message the schema admits must survive the models intact.

    Equality is checked on the models rather than the bytes: pydantic may
    widen an integer to a float where the schema says `number`, which is a
    legal rendering of the same message. What must not happen is a field
    being lost, renamed, or changed in meaning.
    """
    model = decode_object(raw)

    rendered = to_object(model)
    assert is_valid(rendered)

    reparsed = decode_object(rendered)
    assert reparsed == model

    # Idempotent: whatever widening happened, it happened once.
    assert to_object(reparsed) == rendered


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(MESSAGE_STRATEGY)
def test_generated_message_survives_a_string_frame(raw: Any) -> None:
    model = decode_object(raw)
    assert decode(encode(model)) == model


def test_unknown_fields_survive_the_round_trip() -> None:
    """The forward compatibility rule, end to end.

    A brain that dropped unknown fields while relaying would rewrite messages
    from bodies newer than itself, and the flight recorder would hold a
    doctored copy rather than what arrived (SPEC section 4, ADR-0005).
    """
    original = json.loads(
        (Path(__file__).parent / "fixtures" / "protocol" / "valid")
        .joinpath("18-unknown-future-fields.json")
        .read_text(encoding="utf-8")
    )
    rendered = to_object(decode_object(original))

    assert rendered["priority"] == "high"
    assert rendered["hop_count"] == 2
    assert rendered["ts"]["tai_ns"] == 9932000037
    assert rendered["payload"]["checksum"] == "crc32:8a1f20be"
    assert rendered["payload"]["data"]["confidence"] == 0.92
    assert rendered == original


def test_absent_and_null_are_not_confused() -> None:
    """`"error": null` and no `error` key are both legal and must stay distinct."""
    base = {
        "type": "command_result",
        "id": "01JZQK8N4T00000000000000N1",
        "session": "sess_1",
        "seq": 1,
        "ts": {"mono_ns": 1, "utc": "2026-07-29T18:00:00.000Z"},
        "span_id": "spn_1",
        "payload": {"status": "succeeded"},
    }

    without = to_object(decode_object(base))
    assert "error" not in without["payload"]

    explicit = json.loads(json.dumps(base))
    explicit["payload"]["error"] = None
    assert to_object(decode_object(explicit))["payload"]["error"] is None


def test_an_unset_optional_is_omitted_rather_than_nulled() -> None:
    """The trap `without_none` exists for.

    Serialization keeps whatever was explicitly set, which is what preserves
    absent-versus-null. The cost is that passing `trace_id=None` marks it set
    and emits `"trace_id": null`, which the schema refuses because the field
    is a string. This has bitten twice: a reject's `supported` and an event's
    `trace_id`.
    """
    fields = without_none(
        type="event",
        id="01JZQK8N4T00000000000000H1",
        session="sess_1",
        seq=2,
        ts=Timestamp(mono_ns=1, utc="2026-07-29T18:00:00.000Z"),
        trace_id=None,
        span_id=None,
        payload=EventPayload(capability="sys", event="state", data={"state": "ok"}),
    )
    rendered = to_object(EventEnvelope(**fields))

    assert "trace_id" not in rendered
    assert "span_id" not in rendered
    assert is_valid(rendered)


def test_decode_rejects_a_frame_that_is_not_json() -> None:
    with pytest.raises(MalformedFrameError):
        decode("{not json")


def test_decode_rejects_a_schema_invalid_message() -> None:
    """The boundary refuses on the way in, before anything downstream sees it."""
    with pytest.raises(ProtocolValidationError):
        decode_object({"type": "estop", "id": "x", "seq": 1, "ts": {}, "payload": {}})


def test_terminal_status_is_recognised() -> None:
    model = decode_object(
        {
            "type": "command_result",
            "id": "01JZQK8N4T00000000000000N1",
            "session": "sess_1",
            "seq": 1,
            "ts": {"mono_ns": 1, "utc": "2026-07-29T18:00:00.000Z"},
            "span_id": "spn_1",
            "payload": {"status": "running", "progress": 0.5},
        }
    )
    assert not model.payload.is_terminal

    model.payload.status = "succeeded"
    assert model.payload.is_terminal
