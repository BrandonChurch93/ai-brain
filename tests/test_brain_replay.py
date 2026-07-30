"""Replay (ADR-0005, checklist step 2.2).

The done-check: record a synthetic session, replay it, and the reconstructed
sequence is byte-identical. This is the seed of V1 acceptance test 3.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brain.recorder import SESSION_META_TOPIC, FlightRecorder
from brain.replay import read_session
from wire import Manifest, ProtocolValidationError, decode, decode_object, encode
from wire.stamp import now

VALID_FIXTURES = sorted((Path(__file__).parent / "fixtures" / "protocol" / "valid").glob("*.json"))


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


def synthetic_session(path: Path) -> list[str]:
    """Record every fixture message, alternating direction. Returns the wire
    frames in the order they were written."""
    frames: list[str] = []

    with FlightRecorder(path) as recorder:
        recorder.record_session(
            session="sess_synth",
            body_id="mock-01",
            protocol_version="2026-07-29",
            manifest=manifest(),
        )
        for position, fixture in enumerate(VALID_FIXTURES):
            message = decode_object(json.loads(fixture.read_text(encoding="utf-8")))
            direction = "rx" if position % 2 == 0 else "tx"
            recorder.record(
                direction,  # type: ignore[arg-type]
                message,
                session="sess_synth",
                body_id="mock-01",
                t_received=now() if direction == "rx" else None,
            )
            frames.append(encode(message))

    return frames


@pytest.fixture
def recorded(tmp_path: Path) -> tuple[Path, list[str]]:
    path = tmp_path / "synthetic.mcap"
    return path, synthetic_session(path)


# The done-check


def test_replay_is_byte_identical(recorded: tuple[Path, list[str]]) -> None:
    """Every frame, in order, byte for byte."""
    path, original = recorded
    assert list(read_session(path).frames()) == original


def test_replay_preserves_order(recorded: tuple[Path, list[str]]) -> None:
    path, _ = recorded
    session = read_session(path)

    recorded_types = [record.type for record in session]
    expected = [
        decode_object(json.loads(fixture.read_text(encoding="utf-8"))).type
        for fixture in VALID_FIXTURES
    ]
    assert recorded_types == expected


def test_replay_reconstructs_every_message(recorded: tuple[Path, list[str]]) -> None:
    path, original = recorded
    assert len(read_session(path)) == len(original) == len(VALID_FIXTURES)


def test_replayed_messages_equal_the_originals(recorded: tuple[Path, list[str]]) -> None:
    """Not just the bytes: the decoded models compare equal too."""
    path, original = recorded
    for record, frame in zip(read_session(path), original, strict=True):
        assert record.message == decode(frame)


# What replay carries


def test_session_meta_is_recovered(recorded: tuple[Path, list[str]]) -> None:
    path, _ = recorded
    meta = read_session(path).meta

    assert meta is not None
    assert meta.session == "sess_synth"
    assert meta.body_id == "mock-01"
    assert meta.protocol_version == "2026-07-29"
    assert meta.manifest.body_id == "mock-01"
    assert [c.id for c in meta.manifest.capabilities] == ["sys", "drive0"]


def test_direction_survives_the_round_trip(recorded: tuple[Path, list[str]]) -> None:
    path, _ = recorded
    session = read_session(path)

    assert len(session.inbound()) + len(session.outbound()) == len(session)
    assert {record.direction for record in session} == {"rx", "tx"}


def test_timestamps_survive_the_round_trip(recorded: tuple[Path, list[str]]) -> None:
    path, _ = recorded
    session = read_session(path)

    for record in session.inbound():
        assert record.t_received is not None
        assert record.t_captured.mono_ns >= 0
    for record in session.outbound():
        assert record.t_received is None


def test_indices_are_unique_and_ordered(recorded: tuple[Path, list[str]]) -> None:
    path, _ = recorded
    indices = [record.index for record in read_session(path)]
    assert indices == sorted(indices)
    assert len(set(indices)) == len(indices)


# Re-feeding


def test_feed_replays_in_order(recorded: tuple[Path, list[str]]) -> None:
    path, original = recorded
    seen: list[str] = []

    read_session(path).feed(lambda record: seen.append(record.frame()))

    assert seen == original


# Ordering is not left to timestamps


def test_order_survives_records_sharing_a_timestamp(tmp_path: Path) -> None:
    """The reason the file-wide index exists. These records all carry the
    same t_captured, so timestamps alone could not order them."""
    path = tmp_path / "same-instant.mcap"
    same = {"mono_ns": 5000, "utc": "2026-07-29T18:00:05.000Z"}

    frames: list[str] = []
    with FlightRecorder(path) as recorder:
        for seq in range(2, 12):
            message = decode_object(
                {
                    "type": "heartbeat",
                    "id": f"01JZQK8N4T0000000000000{seq:03d}",
                    "session": "sess_1",
                    "seq": seq,
                    "ts": same,
                    "payload": {"state": "ok", "uptime_ms": seq},
                }
            )
            recorder.record("tx", message, session="sess_1")
            frames.append(encode(message))

    replayed = read_session(path)
    assert list(replayed.frames()) == frames
    assert [record.message.seq for record in replayed] == list(range(2, 12))


# Refusals


def test_an_empty_session_replays_as_nothing(tmp_path: Path) -> None:
    path = tmp_path / "empty.mcap"
    with FlightRecorder(path):
        pass

    session = read_session(path)
    assert len(session) == 0
    assert session.meta is None
    assert session.frames() == ()


def test_a_session_with_no_meta_still_replays(tmp_path: Path) -> None:
    """A body rejected at the handshake never gets a session_meta record."""
    path = tmp_path / "no-meta.mcap"
    with FlightRecorder(path) as recorder:
        recorder.record(
            "tx",
            decode_object(
                {
                    "type": "reject",
                    "id": "01JZQK8N4T00000000000000C1",
                    "seq": 1,
                    "ts": {"mono_ns": 1, "utc": "2026-07-29T18:00:00.000Z"},
                    "payload": {"code": "auth_failed", "message": "nope"},
                }
            ),
        )

    session = read_session(path)
    assert session.meta is None
    assert len(session) == 1
    assert session.records[0].type == "reject"


def test_an_edited_recording_is_caught_on_read(tmp_path: Path) -> None:
    """Replay decodes through the same boundary as live traffic, so a record
    doctored into something illegal fails here rather than halfway through a
    re-run."""
    path = tmp_path / "doctored.mcap"

    with FlightRecorder(path) as recorder:
        recorder.record(
            "tx",
            decode_object(
                {
                    "type": "command",
                    "id": "01JZQK8N4T00000000000000K1",
                    "session": "sess_1",
                    "seq": 2,
                    "ts": {"mono_ns": 1, "utc": "2026-07-29T18:00:00.000Z"},
                    "span_id": "spn_1",
                    "payload": {
                        "capability": "drive0",
                        "action": "set_velocity",
                        "params": {},
                        "ttl_ms": 500,
                    },
                }
            ),
        )

    doctored = _rewrite_first_record(path, tmp_path / "broken.mcap", drop="ttl_ms")

    with pytest.raises(ProtocolValidationError) as caught:
        read_session(doctored)
    assert "ttl_ms" in str(caught.value)


def _rewrite_first_record(source: Path, target: Path, *, drop: str) -> Path:
    """Rebuild a file with `drop` removed from the first message payload."""
    from mcap.reader import make_reader

    with source.open("rb") as handle:
        records = [
            (channel.topic, json.loads(message.data))
            for _schema, channel, message in make_reader(handle).iter_messages()
        ]

    with FlightRecorder(target) as recorder:
        for topic, record in records:
            if topic == SESSION_META_TOPIC:
                continue
            record["message"]["payload"].pop(drop, None)
            # Bypass validation deliberately: this is a corrupted file, which
            # the writer would rightly refuse to produce.
            recorder._write(
                topic,
                record,
                log_time=1,
                publish_time=1,
            )

    return target
