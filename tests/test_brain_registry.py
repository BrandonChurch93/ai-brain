"""Session registry: sequence tracking and t_received stamping (SPEC section 4)."""

from __future__ import annotations

import logging

import pytest

from brain.registry import SeqStatus, SeqTracker, SessionRegistry
from wire import Manifest, Message, decode_object
from wire.stamp import SeqCounter, now

SESSION = "sess_test"


def manifest() -> Manifest:
    return Manifest.model_validate(
        {
            "body_id": "mock-01",
            "hardware_class": "virtual",
            "boot_state": "safe_hold",
            "adapter": {"name": "mock-adapter", "version": "0.1.0"},
            "capabilities": [{"id": "sys", "class": "system", "actions": ["ping"]}],
        }
    )


def heartbeat(seq: int, *, session: str = SESSION) -> Message:
    return decode_object(
        {
            "type": "heartbeat",
            "id": f"01JZQK8N4T0000000000000{seq:03d}",
            "session": session,
            "seq": seq,
            "ts": {"mono_ns": seq * 1000, "utc": "2026-07-29T18:00:00.000Z"},
            "payload": {"state": "ok"},
        }
    )


@pytest.fixture
def registry() -> SessionRegistry:
    reg = SessionRegistry()
    reg.open(
        session=SESSION,
        body_id="mock-01",
        protocol_version="2026-07-29",
        manifest=manifest(),
        first_seq=1,
    )
    return reg


# SeqTracker on its own


def test_consecutive_sequence_is_in_order() -> None:
    tracker = SeqTracker(last=1)
    for seq in (2, 3, 4):
        assert tracker.observe(seq) == (SeqStatus.IN_ORDER, 0)
    assert tracker.last == 4


def test_a_skipped_number_is_a_gap_and_counts_what_was_missed() -> None:
    tracker = SeqTracker(last=1)
    assert tracker.observe(5) == (SeqStatus.GAP, 3)


def test_a_gap_does_not_poison_everything_after_it() -> None:
    """Refusing to advance would report every later message as a gap too,
    turning one fault into an endless stream of them."""
    tracker = SeqTracker(last=1)
    tracker.observe(5)
    assert tracker.observe(6) == (SeqStatus.IN_ORDER, 0)


def test_a_repeated_number_is_a_duplicate() -> None:
    tracker = SeqTracker(last=4)
    assert tracker.observe(4) == (SeqStatus.DUPLICATE, 0)
    assert tracker.observe(2) == (SeqStatus.DUPLICATE, 0)


def test_a_duplicate_does_not_rewind_the_counter() -> None:
    tracker = SeqTracker(last=4)
    tracker.observe(2)
    assert tracker.last == 4
    assert tracker.observe(5) == (SeqStatus.IN_ORDER, 0)


def test_tracker_is_seeded_from_hello() -> None:
    """The body's hello is seq 1, so its next message is seq 2."""
    tracker = SeqTracker(last=1)
    assert tracker.observe(2) == (SeqStatus.IN_ORDER, 0)


# The registry


def test_receive_stamps_t_received(registry: SessionRegistry) -> None:
    reception = registry.receive(SESSION, heartbeat(2))
    assert reception.t_received.mono_ns > 0
    assert reception.t_received.utc.endswith("Z")


def test_caller_supplied_stamp_is_kept(registry: SessionRegistry) -> None:
    """The server stamps before parsing; the registry must not overwrite it."""
    stamp = now()
    reception = registry.receive(SESSION, heartbeat(2), stamp)
    assert reception.t_received is stamp


def test_in_order_message_is_not_suspect(registry: SessionRegistry) -> None:
    reception = registry.receive(SESSION, heartbeat(2))
    assert reception.seq_status is SeqStatus.IN_ORDER
    assert not reception.is_suspect


def test_gap_is_reported_and_logged(
    registry: SessionRegistry, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="brain.registry"):
        reception = registry.receive(SESSION, heartbeat(6))

    assert reception.seq_status is SeqStatus.GAP
    assert reception.missing == 4
    assert reception.is_suspect
    assert "seq gap" in caplog.text
    assert "4 message(s) skipped" in caplog.text


def test_duplicate_is_reported_and_logged(
    registry: SessionRegistry, caplog: pytest.LogCaptureFixture
) -> None:
    registry.receive(SESSION, heartbeat(2))

    with caplog.at_level(logging.WARNING, logger="brain.registry"):
        reception = registry.receive(SESSION, heartbeat(2))

    assert reception.seq_status is SeqStatus.DUPLICATE
    assert reception.is_suspect
    assert "already seen" in caplog.text


def test_duplicate_is_surfaced_rather_than_silently_dropped(
    registry: SessionRegistry,
) -> None:
    """Dropping it here would hide the fault. Command-level deduplication is
    the body's job, keyed on span_id (SPEC section 6.6)."""
    registry.receive(SESSION, heartbeat(2))
    reception = registry.receive(SESSION, heartbeat(2))
    assert reception.message is not None


def test_a_message_naming_another_session_is_flagged(
    registry: SessionRegistry, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="brain.registry"):
        reception = registry.receive(SESSION, heartbeat(2, session="sess_somewhere_else"))

    assert reception.session_mismatch
    assert reception.is_suspect
    assert "names session" in caplog.text


def test_receiving_on_an_unknown_session_is_an_error(registry: SessionRegistry) -> None:
    with pytest.raises(KeyError):
        registry.receive("sess_never_opened", heartbeat(2))


def test_opening_the_same_session_twice_is_an_error(registry: SessionRegistry) -> None:
    with pytest.raises(KeyError):
        registry.open(
            session=SESSION,
            body_id="mock-01",
            protocol_version="2026-07-29",
            manifest=manifest(),
            first_seq=1,
        )


def test_close_removes_the_session(registry: SessionRegistry) -> None:
    assert SESSION in registry
    assert registry.close(SESSION) is not None
    assert SESSION not in registry
    assert len(registry) == 0


def test_close_is_idempotent(registry: SessionRegistry) -> None:
    registry.close(SESSION)
    assert registry.close(SESSION) is None


def test_record_keeps_the_manifest_for_later_grounding(registry: SessionRegistry) -> None:
    """ADR-0003 and ADR-0004: the validator grounds commands against this."""
    record = registry.get(SESSION)
    assert record is not None
    assert [capability.id for capability in record.manifest.capabilities] == ["sys"]


def test_each_session_counts_independently() -> None:
    reg = SessionRegistry()
    for name in ("sess_a", "sess_b"):
        reg.open(
            session=name,
            body_id=f"body-{name}",
            protocol_version="2026-07-29",
            manifest=manifest(),
            first_seq=1,
        )

    reg.receive("sess_a", heartbeat(2, session="sess_a"))
    reg.receive("sess_a", heartbeat(3, session="sess_a"))

    # sess_b has seen nothing, so its seq 2 is still in order.
    assert reg.receive("sess_b", heartbeat(2, session="sess_b")).seq_status is SeqStatus.IN_ORDER
    assert reg.get("sess_a").inbound.last == 3  # type: ignore[union-attr]
    assert reg.get("sess_b").inbound.last == 2  # type: ignore[union-attr]


def test_outbound_counter_starts_at_one_and_increments() -> None:
    counter = SeqCounter()
    assert [counter.take() for _ in range(3)] == [1, 2, 3]
    assert counter.peek == 4


def test_registry_can_adopt_an_existing_outbound_counter() -> None:
    """The handshake already sent `welcome` as seq 1, so the session must
    continue from there rather than restarting and repeating a number."""
    seq = SeqCounter()
    seq.take()

    reg = SessionRegistry()
    record = reg.open(
        session="sess_x",
        body_id="mock-01",
        protocol_version="2026-07-29",
        manifest=manifest(),
        first_seq=1,
        outbound=seq,
    )
    assert record.outbound.take() == 2
