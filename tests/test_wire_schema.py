"""The schema loader and the validator it builds.

These guard the seams that fail quietly: a schema found at the wrong path, a
constant that drifted from the file, or format checking silently switched off.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wire import (
    SUBPROTOCOL,
    SchemaNotFoundError,
    capability_classes,
    is_valid,
    message_types,
    protocol_schema,
    schema_id,
    schema_path,
)
from wire.schema import SCHEMA_PATH_ENV_VAR


def test_schema_resolves_to_the_repo_copy() -> None:
    path = schema_path()
    assert path.is_file()
    assert path.parts[-3:] == ("protocol", "schemas", "protocol.schema.json")


def test_schema_parses_and_is_draft_2020_12() -> None:
    schema = protocol_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$ref"] == "#/$defs/Envelope"
    assert schema_id()


def test_constants_are_read_from_the_schema_not_restated() -> None:
    """If these were hand-written they could drift. They are derived, so they cannot."""
    raw = json.loads(schema_path().read_text(encoding="utf-8"))
    assert message_types() == frozenset(raw["$defs"]["Envelope"]["properties"]["type"]["enum"])
    assert capability_classes() == frozenset(
        raw["$defs"]["Capability"]["properties"]["class"]["enum"]
    )
    assert len(message_types()) == 12


def test_subprotocol_carries_no_project_name() -> None:
    """ADR-0000 rule 4: the protocol identifies itself generically."""
    assert SUBPROTOCOL == "body-adapter-protocol.v1"


def test_env_override_points_the_loader_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = tmp_path / "protocol.schema.json"
    copy.write_text(schema_path().read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv(SCHEMA_PATH_ENV_VAR, str(copy))
    assert schema_path() == copy


def test_env_override_pointing_nowhere_is_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SCHEMA_PATH_ENV_VAR, "/nonexistent/protocol.schema.json")
    with pytest.raises(SchemaNotFoundError):
        schema_path()


def _minimal_heartbeat() -> dict:
    return {
        "type": "heartbeat",
        "id": "01JZQK8N4T00000000000000F1",
        "session": "sess_1",
        "seq": 1,
        "ts": {"mono_ns": 1, "utc": "2026-07-29T18:00:00.000Z"},
        "payload": {"state": "ok"},
    }


def test_format_checking_is_active() -> None:
    """`ts.utc` is `format: date-time`. jsonschema ignores formats unless a
    checker is attached, so this asserts the checker is attached."""
    message = _minimal_heartbeat()
    assert is_valid(message)

    message["ts"]["utc"] = "yesterday afternoon"
    assert not is_valid(message)


def test_unknown_fields_are_accepted_everywhere() -> None:
    """SPEC section 4 and 9.1: additive evolution depends on this staying true."""
    message = _minimal_heartbeat()
    message["invented_later"] = {"anything": True}
    message["payload"]["also_invented_later"] = 42
    message["ts"]["tai_ns"] = 7
    assert is_valid(message)
