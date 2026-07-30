"""The flight recorder (ADR-0005, checklist step 2.1).

Every assertion reads the file back through the `mcap` library rather than
through the writer's own bookkeeping. A recorder that agrees with itself
proves nothing; the question is whether the bytes on disk are a valid MCAP
file that another tool can open.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcap.reader import make_reader

from brain.config import ServerConfig
from brain.recorder import (
    SESSION_META_TOPIC,
    FlightRecorder,
    topic_for,
)
from wire import Manifest, decode_object, message_types, schema_id
from wire.stamp import epoch_ns, now

EXPECTED_CHANNELS = 13  # twelve message types plus session_meta


def manifest() -> Manifest:
    return Manifest.model_validate(
        {
            "body_id": "mock-01",
            "hardware_class": "virtual",
            "boot_state": "safe_hold",
            "adapter": {"name": "mock-adapter", "version": "0.1.0"},
            "capabilities": [
                {"id": "sys", "class": "system", "actions": ["ping"]},
                {"id": "drive0", "class": "differential_drive", "actions": ["set_velocity"]},
            ],
        }
    )


def heartbeat(seq: int, session: str = "sess_1"):
    return decode_object(
        {
            "type": "heartbeat",
            "id": f"01JZQK8N4T0000000000000{seq:03d}",
            "session": session,
            "seq": seq,
            "ts": {"mono_ns": seq * 1000, "utc": f"2026-07-29T18:00:{seq:02d}.000Z"},
            "payload": {"state": "ok", "uptime_ms": seq * 100},
        }
    )


def command(span_id: str, session: str = "sess_1"):
    return decode_object(
        {
            "type": "command",
            "id": "01JZQK8N4T00000000000000K1",
            "session": session,
            "seq": 7,
            "ts": {"mono_ns": 7000, "utc": "2026-07-29T18:00:07.000Z"},
            "trace_id": "trc_patrol",
            "span_id": span_id,
            "payload": {
                "capability": "drive0",
                "action": "set_velocity",
                "params": {"linear_mps": 0.2},
                "ttl_ms": 500,
            },
        }
    )


def read_back(path: Path) -> tuple[Any, list[tuple[str, dict]]]:
    """Reopen the file and return (summary, [(topic, record), ...])."""
    with path.open("rb") as handle:
        reader = make_reader(handle)
        summary = reader.get_summary()
        messages = [
            (channel.topic, json.loads(message.data))
            for _schema, channel, message in reader.iter_messages()
        ]
    return summary, messages


@pytest.fixture
def recorded(tmp_path: Path) -> Path:
    path = tmp_path / "session.mcap"
    with FlightRecorder(path) as recorder:
        recorder.record_session(
            session="sess_1",
            body_id="mock-01",
            protocol_version="2026-07-29",
            manifest=manifest(),
        )
        recorder.record("rx", heartbeat(2), session="sess_1", body_id="mock-01", t_received=now())
        recorder.record("rx", heartbeat(3), session="sess_1", body_id="mock-01", t_received=now())
        recorder.record("tx", command("spn_1"), session="sess_1", body_id="mock-01")
    return path


# The file itself


def test_the_file_is_readable_mcap(recorded: Path) -> None:
    summary, _ = read_back(recorded)
    assert summary is not None


def test_every_message_type_gets_a_channel_even_when_unused(recorded: Path) -> None:
    """A channel that exists but is silent and a channel that is missing mean
    different things. Only two types were recorded here."""
    summary, _ = read_back(recorded)

    topics = {channel.topic for channel in summary.channels.values()}
    expected = {topic_for(name) for name in message_types()} | {SESSION_META_TOPIC}

    assert topics == expected
    assert len(topics) == EXPECTED_CHANNELS


def test_message_count_is_what_was_written(recorded: Path) -> None:
    summary, messages = read_back(recorded)

    assert summary.statistics is not None
    assert summary.statistics.message_count == 4
    assert len(messages) == 4


def test_per_channel_counts(recorded: Path) -> None:
    summary, _ = read_back(recorded)

    by_topic = {
        summary.channels[channel_id].topic: count
        for channel_id, count in summary.statistics.channel_message_counts.items()  # type: ignore[union-attr]
    }
    assert by_topic == {SESSION_META_TOPIC: 1, "/heartbeat": 2, "/command": 1}


def test_the_protocol_schema_is_embedded(recorded: Path) -> None:
    """The file explains itself: whoever opens it later gets the contract it
    was recorded under, without needing this repo at that version."""
    summary, _ = read_back(recorded)

    schemas = {schema.name: schema for schema in summary.schemas.values()}
    assert "body-adapter-protocol" in schemas
    assert schemas["body-adapter-protocol"].encoding == "jsonschema"

    embedded = json.loads(schemas["body-adapter-protocol"].data)
    assert embedded["$id"] == schema_id()
    assert "Envelope" in embedded["$defs"]


def test_the_profile_carries_no_project_name(recorded: Path) -> None:
    """ADR-0000 rule 5."""
    with recorded.open("rb") as handle:
        header = make_reader(handle).get_header()
    assert header.profile == "body-adapter-protocol"


# What each record holds


def test_session_meta_carries_the_negotiated_version_and_manifest(recorded: Path) -> None:
    _, messages = read_back(recorded)
    meta = next(record for topic, record in messages if topic == SESSION_META_TOPIC)

    assert meta["session"] == "sess_1"
    assert meta["body_id"] == "mock-01"
    assert meta["protocol_version"] == "2026-07-29"
    assert meta["schema_id"] == schema_id()
    assert meta["manifest"]["body_id"] == "mock-01"
    assert [c["id"] for c in meta["manifest"]["capabilities"]] == ["sys", "drive0"]


def test_records_carry_direction(recorded: Path) -> None:
    _, messages = read_back(recorded)

    directions = {
        (topic, record["direction"]) for topic, record in messages if topic != SESSION_META_TOPIC
    }
    assert directions == {("/heartbeat", "rx"), ("/command", "tx")}


def test_inbound_records_carry_both_timestamps(recorded: Path) -> None:
    """ADR-0005 point 2: t_captured and t_received, each with both clocks."""
    _, messages = read_back(recorded)
    beat = next(record for topic, record in messages if topic == "/heartbeat")

    assert beat["t_captured"] == {"mono_ns": 2000, "utc": "2026-07-29T18:00:02.000Z"}
    assert beat["t_received"] is not None
    assert beat["t_received"]["mono_ns"] > 0
    assert beat["t_received"]["utc"].endswith("Z")


def test_outbound_records_have_no_t_received(recorded: Path) -> None:
    """Nothing received it here. Inventing a value would be a lie in the log."""
    _, messages = read_back(recorded)
    sent = next(record for topic, record in messages if topic == "/command")
    assert sent["t_received"] is None


def test_records_hoist_seq_trace_and_span(recorded: Path) -> None:
    """ADR-0005 point 3: filterable without parsing the nested envelope."""
    _, messages = read_back(recorded)
    sent = next(record for topic, record in messages if topic == "/command")

    assert sent["seq"] == 7
    assert sent["trace_id"] == "trc_patrol"
    assert sent["span_id"] == "spn_1"


def test_the_full_envelope_is_kept_verbatim(recorded: Path) -> None:
    _, messages = read_back(recorded)
    sent = next(record for topic, record in messages if topic == "/command")

    assert sent["message"]["payload"]["params"] == {"linear_mps": 0.2}
    assert sent["message"]["payload"]["ttl_ms"] == 500
    assert sent["message"]["type"] == "command"


def test_mcap_timestamps_are_epoch_nanoseconds(recorded: Path) -> None:
    """Monotonic clocks count from a per-machine origin, so they are not
    comparable across devices. MCAP needs one timeline that is."""
    with recorded.open("rb") as handle:
        stamps = [
            (message.log_time, message.publish_time)
            for _schema, channel, message in make_reader(handle).iter_messages()
            if channel.topic == "/heartbeat"
        ]

    expected = epoch_ns(heartbeat(2).ts)
    assert stamps[0][1] == expected
    assert stamps[0][0] > 1_700_000_000_000_000_000  # a plausible epoch, not a mono origin


def test_sequence_numbers_are_per_channel(recorded: Path) -> None:
    with recorded.open("rb") as handle:
        beats = [
            message.sequence
            for _schema, channel, message in make_reader(handle).iter_messages()
            if channel.topic == "/heartbeat"
        ]
    assert beats == [1, 2]


# Lifecycle


def test_recorder_reports_its_own_counts(tmp_path: Path) -> None:
    recorder = FlightRecorder(tmp_path / "a.mcap")
    recorder.open()
    recorder.record("rx", heartbeat(2), session="sess_1")
    stats = recorder.close()

    assert stats.messages == 1
    assert stats.channels == EXPECTED_CHANNELS


def test_recorder_creates_the_directory(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "deeper" / "session.mcap"
    with FlightRecorder(path):
        pass
    assert path.is_file()


def test_writing_before_open_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="not open"):
        FlightRecorder(tmp_path / "a.mcap").record("rx", heartbeat(2))


def test_opening_twice_is_an_error(tmp_path: Path) -> None:
    recorder = FlightRecorder(tmp_path / "a.mcap")
    recorder.open()
    with pytest.raises(RuntimeError, match="already open"):
        recorder.open()
    recorder.close()


def test_an_empty_session_is_still_a_valid_file(tmp_path: Path) -> None:
    """A body that connects and says nothing must still leave a readable log."""
    path = tmp_path / "empty.mcap"
    with FlightRecorder(path):
        pass

    summary, messages = read_back(path)
    assert messages == []
    assert len(summary.channels) == EXPECTED_CHANNELS


# Config


def test_session_path_lands_under_the_configured_log_dir() -> None:
    config = ServerConfig.from_env({"BRAIN_LOG_DIR": "/tmp/somewhere"})
    assert config.session_path("sess_1", "mock-01") == Path("/tmp/somewhere/mock-01-sess_1.mcap")


def test_log_dir_defaults_when_unset() -> None:
    assert ServerConfig.from_env({}).log_dir == Path("./var/logs")
