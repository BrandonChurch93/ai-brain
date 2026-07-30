"""Heartbeats and lease detection (SPEC sections 6.4 and 8.1, ADR-0006)."""

from __future__ import annotations

import asyncio
import logging

import pytest

from brain.heartbeat import LeaseWatch, brain_heartbeat, heartbeat_loop
from brain.registry import BODY_LOST, SessionRegistry
from wire import Manifest, decode_object, to_object
from wire.stamp import SeqCounter

MS = 1_000_000


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


class FakeClock:
    """Monotonic nanoseconds under test control."""

    def __init__(self) -> None:
        self.ns = 0

    def __call__(self) -> int:
        return self.ns

    def advance_ms(self, ms: float) -> None:
        self.ns += int(ms * MS)


# LeaseWatch


def test_a_fresh_watch_is_not_expired() -> None:
    clock = FakeClock()
    assert not LeaseWatch(3000, clock).expired


def test_lease_expires_exactly_at_the_limit() -> None:
    clock = FakeClock()
    watch = LeaseWatch(3000, clock)

    clock.advance_ms(2999)
    assert not watch.expired

    clock.advance_ms(1)
    assert watch.expired


def test_a_heartbeat_renews_the_lease() -> None:
    clock = FakeClock()
    watch = LeaseWatch(3000, clock)

    clock.advance_ms(2900)
    watch.beat()
    clock.advance_ms(2900)

    assert not watch.expired
    assert watch.silent_ms == pytest.approx(2900)


def test_silence_is_measured_from_the_last_beat() -> None:
    clock = FakeClock()
    watch = LeaseWatch(3000, clock)
    clock.advance_ms(1500)
    assert watch.silent_ms == pytest.approx(1500)


def test_a_nonpositive_lease_is_refused() -> None:
    with pytest.raises(ValueError, match="lease must be positive"):
        LeaseWatch(0, FakeClock())


def test_the_watch_uses_a_monotonic_clock_not_the_wall_clock() -> None:
    """A wall-clock lease would expire an entire fleet at once on an NTP step."""
    clock = FakeClock()
    watch = LeaseWatch(1000, clock)
    clock.ns -= 10_000 * MS  # a wall clock could do this; monotonic cannot
    assert not watch.expired


# The outbound heartbeat message


def test_brain_heartbeat_is_a_legal_message() -> None:
    message = brain_heartbeat("active", "sess_1", SeqCounter())
    rendered = to_object(message)

    assert rendered["type"] == "heartbeat"
    assert rendered["payload"] == {"state": "active"}
    assert rendered["session"] == "sess_1"


def test_brain_heartbeat_can_report_degraded() -> None:
    """SPEC section 8.5: DEGRADED is visible to bodies only as this."""
    assert to_object(brain_heartbeat("degraded", "sess_1", SeqCounter()))["payload"] == {
        "state": "degraded"
    }


def test_brain_heartbeat_consumes_the_session_counter() -> None:
    seq = SeqCounter()
    seq.take()
    assert brain_heartbeat("active", "sess_1", seq).seq == 2


# The loop


async def test_loop_beats_on_the_interval() -> None:
    clock = FakeClock()
    sent: list[str] = []

    task = asyncio.create_task(
        heartbeat_loop(
            session="sess_1",
            interval_ms=5,
            watch=LeaseWatch(10_000, clock),
            seq=SeqCounter(),
            send=lambda message: sent.append(message.payload.state),
            brain_state=lambda: "active",
            on_lost=lambda silent: None,
        )
    )

    await asyncio.sleep(0.06)
    task.cancel()

    assert len(sent) >= 3
    assert set(sent) == {"active"}


async def test_loop_reports_a_lease_miss_once(caplog: pytest.LogCaptureFixture) -> None:
    clock = FakeClock()
    losses: list[float] = []

    with caplog.at_level(logging.WARNING, logger="brain.heartbeat"):
        task = asyncio.create_task(
            heartbeat_loop(
                session="sess_1",
                interval_ms=5,
                watch=LeaseWatch(20, clock),
                seq=SeqCounter(),
                send=lambda message: clock.advance_ms(10),
                brain_state=lambda: "active",
                on_lost=lambda silent: losses.append(silent),
            )
        )
        await asyncio.sleep(0.08)
        task.cancel()

    assert len(losses) == 1, "a lease miss must not re-fire on every tick"
    assert losses[0] >= 20
    assert "lease missed" in caplog.text


async def test_loop_notices_a_body_coming_back() -> None:
    clock = FakeClock()
    watch = LeaseWatch(20, clock)
    events: list[str] = []

    def send(_message: object) -> None:
        clock.advance_ms(10)

    task = asyncio.create_task(
        heartbeat_loop(
            session="sess_1",
            interval_ms=5,
            watch=watch,
            seq=SeqCounter(),
            send=send,
            brain_state=lambda: "active",
            on_lost=lambda silent: events.append("lost"),
            on_recovered=lambda: events.append("recovered"),
        )
    )

    while "lost" not in events:
        await asyncio.sleep(0.005)

    watch.beat()
    while "recovered" not in events:
        await asyncio.sleep(0.005)

    task.cancel()
    assert events[:2] == ["lost", "recovered"]


async def test_a_failing_send_does_not_kill_the_loop() -> None:
    """A socket write can fail at any moment; the lease must still be watched."""
    clock = FakeClock()
    losses: list[float] = []

    def send(_message: object) -> None:
        clock.advance_ms(10)
        raise ConnectionResetError("socket went away")

    task = asyncio.create_task(
        heartbeat_loop(
            session="sess_1",
            interval_ms=5,
            watch=LeaseWatch(20, clock),
            seq=SeqCounter(),
            send=send,
            brain_state=lambda: "active",
            on_lost=lambda silent: losses.append(silent),
        )
    )

    await asyncio.sleep(0.08)
    task.cancel()

    assert losses, "lease miss went unreported because the send raised"


# Failing outstanding spans


def command(span_id: str, session: str = "sess_1"):
    return decode_object(
        {
            "type": "command",
            "id": f"01JZQK8N4T00000000000{span_id:>06}",
            "session": session,
            "seq": 2,
            "ts": {"mono_ns": 1, "utc": "2026-07-29T18:00:00.000Z"},
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


def result(span_id: str, status: str, seq: int, session: str = "sess_1"):
    return decode_object(
        {
            "type": "command_result",
            "id": f"01JZQK8N4T0000000000R{span_id:>05}",
            "session": session,
            "seq": seq,
            "ts": {"mono_ns": 2, "utc": "2026-07-29T18:00:01.000Z"},
            "trace_id": "trc_patrol",
            "span_id": span_id,
            "payload": {"status": status},
        }
    )


@pytest.fixture
def registry() -> SessionRegistry:
    reg = SessionRegistry()
    reg.open(
        session="sess_1",
        body_id="mock-01",
        protocol_version="2026-07-29",
        manifest=manifest(),
        first_seq=1,
    )
    return reg


def test_a_sent_command_becomes_an_outstanding_span(registry: SessionRegistry) -> None:
    registry.open_span("sess_1", command("spn_1"))

    outstanding = registry.outstanding("sess_1")
    assert [span.span_id for span in outstanding] == ["spn_1"]
    assert outstanding[0].capability == "drive0"
    assert outstanding[0].ttl_ms == 500


def test_a_terminal_result_closes_the_span(registry: SessionRegistry) -> None:
    registry.open_span("sess_1", command("spn_1"))
    registry.receive("sess_1", result("spn_1", "succeeded", 2))

    assert registry.outstanding("sess_1") == ()


def test_a_non_terminal_result_leaves_the_span_open(registry: SessionRegistry) -> None:
    """SPEC section 6.7: `running` is not the end of a span."""
    registry.open_span("sess_1", command("spn_1"))
    registry.receive("sess_1", result("spn_1", "running", 2))

    assert len(registry.outstanding("sess_1")) == 1


def test_a_second_terminal_result_is_logged(
    registry: SessionRegistry, caplog: pytest.LogCaptureFixture
) -> None:
    """SPEC section 6.7 allows exactly one terminal result per span."""
    registry.open_span("sess_1", command("spn_1"))
    registry.receive("sess_1", result("spn_1", "succeeded", 2))

    with caplog.at_level(logging.WARNING, logger="brain.registry"):
        registry.receive("sess_1", result("spn_1", "succeeded", 3))

    assert "already-closed span" in caplog.text


def test_marking_lost_fails_every_outstanding_span(registry: SessionRegistry) -> None:
    for span_id in ("spn_1", "spn_2", "spn_3"):
        registry.open_span("sess_1", command(span_id))
    registry.receive("sess_1", result("spn_2", "succeeded", 2))

    outcomes = registry.mark_lost("sess_1", message="no heartbeat for 3000ms")

    assert {outcome.span.span_id for outcome in outcomes} == {"spn_1", "spn_3"}
    assert {outcome.status for outcome in outcomes} == {"failed"}
    assert {outcome.code for outcome in outcomes} == {BODY_LOST}
    assert registry.outstanding("sess_1") == ()


def test_marking_lost_sets_the_flag(registry: SessionRegistry) -> None:
    registry.mark_lost("sess_1", message="gone")
    record = registry.get("sess_1")
    assert record is not None
    assert record.lost


def test_marking_lost_twice_reports_once(
    registry: SessionRegistry, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="brain.registry"):
        registry.mark_lost("sess_1", message="gone")
        assert "LOST" in caplog.text

        caplog.clear()
        registry.mark_lost("sess_1", message="gone")
        assert "LOST" not in caplog.text


def test_a_recovered_body_is_marked_live_again(registry: SessionRegistry) -> None:
    registry.mark_lost("sess_1", message="gone")
    assert registry.mark_live("sess_1")

    record = registry.get("sess_1")
    assert record is not None
    assert not record.lost


def test_marking_live_when_already_live_is_a_no_op(registry: SessionRegistry) -> None:
    assert not registry.mark_live("sess_1")


def test_recovery_does_not_resurrect_failed_spans(registry: SessionRegistry) -> None:
    """The spans were given up on. A body coming back does not un-fail them,
    and it unlatches nothing on the body either (SPEC section 8.2)."""
    registry.open_span("sess_1", command("spn_1"))
    registry.mark_lost("sess_1", message="gone")
    registry.mark_live("sess_1")

    assert registry.outstanding("sess_1") == ()
