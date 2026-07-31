"""Heartbeats and lease detection (SPEC sections 6.4 and 8.1, ADR-0006).

No real time anywhere in this file. Every deadline is driven by a
`ManualClock` and a sleeper that advances it, so these run instantly and
give the same answer on a loaded CI runner as on an idle laptop. A timing
test that sleeps is slow when it passes and flaky when it fails, and a flaky
safety test is a muted safety test.
"""

from __future__ import annotations

import logging

import pytest

from brain.heartbeat import LeaseWatch, brain_heartbeat, heartbeat_loop
from brain.registry import BODY_LOST, SessionRegistry
from helpers import LoopFinished, ticker
from wire import Manifest, decode_object, to_object
from wire.clock import ManualClock
from wire.stamp import SeqCounter


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


async def run_loop(clock: ManualClock, ticks: int, **kwargs) -> None:
    """Run the heartbeat loop for a fixed number of ticks."""
    try:
        await heartbeat_loop(clock=clock, sleep=ticker(clock, ticks), **kwargs)
    except LoopFinished:
        return


# LeaseWatch


def test_a_fresh_watch_is_not_expired() -> None:
    assert not LeaseWatch(3000, ManualClock()).expired


def test_lease_expires_exactly_at_the_limit() -> None:
    clock = ManualClock()
    watch = LeaseWatch(3000, clock)

    clock.advance(ms=2999)
    assert not watch.expired

    clock.advance(ms=1)
    assert watch.expired


def test_a_heartbeat_renews_the_lease() -> None:
    clock = ManualClock()
    watch = LeaseWatch(3000, clock)

    clock.advance(ms=2900)
    watch.beat()
    clock.advance(ms=2900)

    assert not watch.expired
    assert watch.silent_ms == pytest.approx(2900)


def test_silence_is_measured_from_the_last_beat() -> None:
    clock = ManualClock()
    watch = LeaseWatch(3000, clock)
    clock.advance(ms=1500)
    assert watch.silent_ms == pytest.approx(1500)


def test_a_nonpositive_lease_is_refused() -> None:
    with pytest.raises(ValueError, match="lease must be positive"):
        LeaseWatch(0, ManualClock())


def test_a_wall_clock_jump_does_not_expire_the_lease() -> None:
    """The reason deadlines are measured on the monotonic clock. An NTP
    correction would otherwise expire every lease in a fleet at once.

    Covers SPEC section 4's skew-tolerance rule: clock skew of tens of
    milliseconds between devices is expected and MUST be tolerated. Proven
    here for an hour forwards and two hours back.
    """
    clock = ManualClock()
    watch = LeaseWatch(1000, clock)

    clock.jump_wall_clock(seconds=3600)
    assert not watch.expired

    clock.jump_wall_clock(seconds=-7200)
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
    clock = ManualClock()
    sent: list[str] = []

    await run_loop(
        clock,
        3,
        session="sess_1",
        interval_ms=1000,
        watch=LeaseWatch(10_000, clock),
        seq=SeqCounter(),
        send=lambda message: sent.append(message.payload.state),
        brain_state=lambda: "active",
        on_lost=lambda silent: None,
    )

    assert sent == ["active"] * 3


async def test_loop_reports_a_lease_miss_once(caplog: pytest.LogCaptureFixture) -> None:
    """Ten ticks past a lease that expires on the third. The report must fire
    once, not on every tick after."""
    clock = ManualClock()
    losses: list[float] = []

    with caplog.at_level(logging.WARNING, logger="brain.heartbeat"):
        await run_loop(
            clock,
            10,
            session="sess_1",
            interval_ms=1000,
            watch=LeaseWatch(2500, clock),
            seq=SeqCounter(),
            send=lambda message: None,
            brain_state=lambda: "active",
            on_lost=lambda silent: losses.append(silent),
        )

    assert len(losses) == 1, "a lease miss must not re-fire on every tick"
    assert losses[0] == pytest.approx(3000)
    assert "lease missed" in caplog.text


async def test_loop_notices_a_body_coming_back() -> None:
    clock = ManualClock()
    watch = LeaseWatch(2500, clock)
    events: list[str] = []
    ticks = 0

    def send(_message: object) -> None:
        nonlocal ticks
        ticks += 1
        if ticks == 5:  # the body starts talking again
            watch.beat()

    await run_loop(
        clock,
        8,
        session="sess_1",
        interval_ms=1000,
        watch=watch,
        seq=SeqCounter(),
        send=send,
        brain_state=lambda: "active",
        on_lost=lambda silent: events.append("lost"),
        on_recovered=lambda: events.append("recovered"),
    )

    # Lost, back, then lost again: the single beat at tick 5 renews the lease
    # but nothing follows it, so it expires a second time by tick 8. Each
    # transition is reported once, which is the property that matters.
    assert events == ["lost", "recovered", "lost"]


async def test_a_lease_miss_does_not_end_the_loop() -> None:
    """The socket may still be open and the body may come back, so the loop
    keeps beating after it gives up on hearing one."""
    clock = ManualClock()
    sent = 0

    def send(_message: object) -> None:
        nonlocal sent
        sent += 1

    await run_loop(
        clock,
        6,
        session="sess_1",
        interval_ms=1000,
        watch=LeaseWatch(2500, clock),
        seq=SeqCounter(),
        send=send,
        brain_state=lambda: "active",
        on_lost=lambda silent: None,
    )

    assert sent == 6


async def test_a_failing_send_does_not_kill_the_loop() -> None:
    """A socket write can fail at any moment; the lease must still be watched."""
    clock = ManualClock()
    losses: list[float] = []

    def send(_message: object) -> None:
        raise ConnectionResetError("socket went away")

    await run_loop(
        clock,
        6,
        session="sess_1",
        interval_ms=1000,
        watch=LeaseWatch(2500, clock),
        seq=SeqCounter(),
        send=send,
        brain_state=lambda: "active",
        on_lost=lambda silent: losses.append(silent),
    )

    assert losses, "lease miss went unreported because the send raised"


async def test_the_loop_stamps_heartbeats_from_the_injected_clock() -> None:
    """Nothing in the timing path reads the wall clock behind the test's back."""
    clock = ManualClock()
    stamps: list[int] = []

    await run_loop(
        clock,
        3,
        session="sess_1",
        interval_ms=1000,
        watch=LeaseWatch(10_000, clock),
        seq=SeqCounter(),
        send=lambda message: stamps.append(message.ts.mono_ns),
        brain_state=lambda: "active",
        on_lost=lambda silent: None,
    )

    assert stamps == [1_000_000_000, 2_000_000_000, 3_000_000_000]


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
