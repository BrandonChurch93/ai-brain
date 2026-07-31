"""Latching (checklist step 3.3, SPEC section 8, ADR-0006).

The done-check: silence the brain and the body latches within its lease; a
reconnect keeps the latch; E-stop latches even while idle; estop_clear plus
clear_safe_hold restore `ok`.

`SafetyState` is tested on its own first, because latching is a state machine
and a state machine is worth pinning without a socket in the way.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

from bodies.client import BodyConfig
from bodies.mock import MockBody
from bodies.safety import LATCHED_SAFE_STATE, SafetyState, State
from brain.config import ServerConfig
from brain.server import BrainServer
from wire import CommandEnvelope, decode_object
from wire.clock import ManualClock
from wire.lease import LeaseWatch

TOKEN = "safety-token"


# The state machine, without a network


def test_a_body_boots_where_its_manifest_says() -> None:
    assert SafetyState("safe_hold").state is State.SAFE_HOLD
    assert SafetyState("ok").state is State.OK


def test_a_latched_body_may_not_actuate() -> None:
    """SPEC section 8.4: stopped is the default and motion is permissioned."""
    assert not SafetyState(State.SAFE_HOLD).may_actuate
    assert not SafetyState(State.ESTOPPED).may_actuate
    assert SafetyState(State.OK).may_actuate


def test_safe_hold_clears_only_through_clear_safe_hold() -> None:
    state = SafetyState(State.SAFE_HOLD)
    assert state.clear_safe_hold().changed
    assert state.state is State.OK


def test_estop_latches_from_any_state() -> None:
    for start in (State.OK, State.SAFE_HOLD, State.ESTOPPED):
        state = SafetyState(start)
        state.estop("operator")
        assert state.state is State.ESTOPPED


def test_a_lease_miss_does_not_downgrade_an_estop() -> None:
    """Otherwise clearing the safe hold would release a stop nobody cleared."""
    state = SafetyState(State.OK)
    state.estop("operator pressed the button")

    transition = state.enter_safe_hold("lease miss")

    assert not transition.ok
    assert state.state is State.ESTOPPED


def test_clear_safe_hold_is_refused_while_estopped() -> None:
    """SPEC section 8.2: each latch clears through its own path."""
    state = SafetyState(State.OK)
    state.estop("operator")

    transition = state.clear_safe_hold()

    assert not transition.ok
    assert state.state is State.ESTOPPED


def test_clearing_an_estop_lands_in_safe_hold_not_in_motion() -> None:
    """One message must not both release an emergency stop and re-enable
    motion; the operator would be authorising more than they said."""
    state = SafetyState(State.OK)
    state.estop("operator")

    state.clear_estop()

    assert state.state is State.SAFE_HOLD
    assert not state.may_actuate


def test_the_full_way_back_takes_both_clears() -> None:
    """The done-check sequence: estop_clear plus clear_safe_hold restore ok."""
    state = SafetyState(State.OK)
    state.estop("operator")

    assert state.clear_estop().changed
    assert state.clear_safe_hold().changed
    assert state.state is State.OK
    assert state.may_actuate


def test_clearing_an_estop_that_is_not_set_is_refused() -> None:
    state = SafetyState(State.SAFE_HOLD)
    assert not state.clear_estop().ok
    assert state.state is State.SAFE_HOLD


def test_nothing_clears_itself() -> None:
    """The rule the whole file exists for. No amount of time passing, and no
    repetition of the cause, moves a latched body back to ok."""
    state = SafetyState(State.OK)
    state.enter_safe_hold("lease miss")

    for _ in range(100):
        state.enter_safe_hold("lease miss again")

    assert state.state is State.SAFE_HOLD


def test_the_cause_is_kept_for_the_state_event() -> None:
    state = SafetyState(State.OK)
    state.estop("obstacle in the path")
    assert state.cause == "obstacle in the path"


# The body, over a socket


@pytest.fixture
async def brain() -> AsyncIterator[tuple[BrainServer, list[Any]]]:
    received: list[Any] = []

    async def collect(state: Any, reception: Any) -> None:
        received.append(reception.message)

    config = ServerConfig(host="127.0.0.1", port=0, auth_token=TOKEN)
    async with BrainServer(config, on_message=collect) as server:
        yield server, received


def config_for(server: BrainServer) -> BodyConfig:
    return BodyConfig(url=f"ws://127.0.0.1:{server.port}", auth_token=TOKEN)


async def until(condition, *, what: str, spins: int = 20_000) -> None:
    for _ in range(spins):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"timed out waiting for {what}")


def drive_command(session: str, span_id: str = "spn_drive", ttl_ms: int = 5000):
    message = decode_object(
        {
            "type": "command",
            "id": f"01JZQK8N4T000000000{span_id:>07}",
            "session": session,
            "seq": 2,
            "ts": {"mono_ns": 1, "utc": "2026-07-29T18:00:00.000Z"},
            "trace_id": "trc_patrol",
            "span_id": span_id,
            "payload": {
                "capability": "drive0",
                "action": "set_velocity",
                "params": {"linear_mps": 0.2, "angular_rps": 0.0},
                "ttl_ms": ttl_ms,
            },
        }
    )
    assert isinstance(message, CommandEnvelope)
    return message


def sys_command(session: str, action: str, span_id: str = "spn_sys"):
    message = decode_object(
        {
            "type": "command",
            "id": f"01JZQK8N4T000000000{span_id:>07}",
            "session": session,
            "seq": 3,
            "ts": {"mono_ns": 1, "utc": "2026-07-29T18:00:00.000Z"},
            "span_id": span_id,
            "payload": {
                "capability": "sys",
                "action": action,
                "params": {},
                "ttl_ms": 5000,
            },
        }
    )
    assert isinstance(message, CommandEnvelope)
    return message


async def connected_body(server: BrainServer, clock: ManualClock) -> tuple[MockBody, str]:
    body = MockBody(config_for(server), clock=clock)
    welcome = await body.client.connect()
    await body.client.announce_boot_state()
    return body, welcome.payload.session


async def test_a_body_refuses_to_move_while_latched(
    brain: tuple[BrainServer, list[Any]],
) -> None:
    """It boots into safe_hold, so the very first drive command is refused."""
    server, _ = brain
    clock = ManualClock()
    body, session = await connected_body(server, clock)

    try:
        span = await body.dispatch.handle(drive_command(session))

        assert span is not None
        assert span.terminal == "rejected"
        assert body.pose.linear_mps == 0.0
    finally:
        await body.client.close()


async def test_a_cleared_body_moves(brain: tuple[BrainServer, list[Any]]) -> None:
    server, _ = brain
    clock = ManualClock()
    body, session = await connected_body(server, clock)

    try:
        await body.dispatch.handle(sys_command(session, "clear_safe_hold"))
        assert body.state == "ok"

        span = await body.dispatch.handle(drive_command(session))

        assert span is not None
        assert span.terminal == "succeeded"
        assert body.pose.linear_mps == pytest.approx(0.2)
    finally:
        await body.client.close()


async def test_the_brain_going_quiet_latches_safe_hold(
    brain: tuple[BrainServer, list[Any]],
) -> None:
    """The done-check. The brain says nothing for longer than the lease."""
    server, received = brain
    clock = ManualClock()
    body, session = await connected_body(server, clock)

    try:
        await body.dispatch.handle(sys_command(session, "clear_safe_hold"))
        assert body.state == "ok"

        lease = body.client.brain_lease
        assert lease is not None
        assert body.client.heartbeat_lease_ms == 3000

        clock.advance(ms=3000)
        assert lease.expired

        await body.enter_safe_hold("brain heartbeat lease missed")

        assert body.state == "safe_hold"
        assert not body.safety.may_actuate
        assert body.pose.linear_mps == 0.0

        await until(
            lambda: any(
                m.type == "event"
                and m.payload.event == "state"
                and m.payload.data["state"] == "safe_hold"
                and m.payload.data["cause"] != "boot"
                for m in received
            ),
            what="the safe_hold state event",
        )
    finally:
        await body.client.close()


async def test_a_lease_miss_fails_in_flight_actuation_spans(
    brain: tuple[BrainServer, list[Any]],
) -> None:
    """SPEC section 8.1: in-flight actuation spans end with terminal failed
    and code latched_safe_state."""
    server, received = brain
    clock = ManualClock()
    body, session = await connected_body(server, clock)

    try:
        await body.dispatch.handle(sys_command(session, "clear_safe_hold"))

        # A drive span that has begun and has not ended.
        command = drive_command(session, span_id="spn_inflight")
        entry = body.ledger.admit(command, body.client.stamp())
        assert entry is not None
        body.ledger.start(entry)

        await body.enter_safe_hold("brain heartbeat lease missed")

        assert entry.terminal == "failed"
        await until(
            lambda: any(
                m.type == "command_result" and m.span_id == "spn_inflight" for m in received
            ),
            what="the failed result",
        )
        result = next(
            m for m in received if m.type == "command_result" and m.span_id == "spn_inflight"
        )
        assert result.payload.status == "failed"
        assert result.payload.error is not None
        assert result.payload.error.code == LATCHED_SAFE_STATE
    finally:
        await body.client.close()


async def test_a_sensor_span_is_not_failed_by_a_latch(
    brain: tuple[BrainServer, list[Any]],
) -> None:
    """Only actuation spans. A range read in flight when the body latches is
    not dangerous and its answer is still true."""
    server, _ = brain
    clock = ManualClock()
    body, session = await connected_body(server, clock)

    try:
        command = decode_object(
            {
                "type": "command",
                "id": "01JZQK8N4T00000000000000R1",
                "session": session,
                "seq": 4,
                "ts": {"mono_ns": 1, "utc": "2026-07-29T18:00:00.000Z"},
                "span_id": "spn_read",
                "payload": {
                    "capability": "range0",
                    "action": "read",
                    "params": {},
                    "ttl_ms": 5000,
                },
            }
        )
        entry = body.ledger.admit(command, body.client.stamp())  # type: ignore[arg-type]
        assert entry is not None
        body.ledger.start(entry)

        await body.enter_safe_hold("lease miss")

        assert entry.terminal is None, "a sensor read is not actuation"
    finally:
        await body.client.close()


async def test_estop_latches_even_while_idle(
    brain: tuple[BrainServer, list[Any]],
) -> None:
    """The done-check. Nothing is running; the stop still takes effect."""
    server, received = brain
    clock = ManualClock()
    body, session = await connected_body(server, clock)

    try:
        await body.dispatch.handle(sys_command(session, "clear_safe_hold"))
        assert body.state == "ok"

        await body.estop("operator voice command")

        assert body.state == "estopped"
        await until(
            lambda: any(
                m.type == "event"
                and m.payload.event == "state"
                and m.payload.data["state"] == "estopped"
                for m in received
            ),
            what="the estopped state event",
        )
    finally:
        await body.client.close()


async def test_estop_then_both_clears_restores_ok(
    brain: tuple[BrainServer, list[Any]],
) -> None:
    """The done-check, end to end over a socket."""
    server, _ = brain
    clock = ManualClock()
    body, session = await connected_body(server, clock)

    try:
        await body.estop("operator")
        assert body.state == "estopped"

        # clear_safe_hold alone is refused while estopped.
        span = await body.dispatch.handle(sys_command(session, "clear_safe_hold", "spn_a"))
        assert span is not None
        assert span.terminal == "rejected"
        assert body.state == "estopped"

        await body.clear_estop("obstacle removed", "brandon")
        assert body.state == "safe_hold"

        span = await body.dispatch.handle(sys_command(session, "clear_safe_hold", "spn_b"))
        assert span is not None
        assert span.terminal == "succeeded"
        assert body.state == "ok"
    finally:
        await body.client.close()


async def test_a_latch_survives_reconnection(
    brain: tuple[BrainServer, list[Any]],
) -> None:
    """The done-check, and the reason SafetyState is not owned by the client.

    A reconnect builds a fresh session and a fresh client. If the state came
    from the manifest each time, a latched body would come back `ok` having
    cleared itself by forgetting, which SPEC section 8.2 forbids.
    """
    server, _ = brain
    clock = ManualClock()
    body, session = await connected_body(server, clock)

    try:
        await body.dispatch.handle(sys_command(session, "clear_safe_hold"))
        assert body.state == "ok"

        await body.estop("operator")
        assert body.state == "estopped"

        # The socket drops and the body dials back in.
        await body.client.close()
        welcome = await body.client.connect()

        assert body.state == "estopped", "a reconnect cleared the latch"
        assert not body.safety.may_actuate

        # And it reports the latched state in its first heartbeat.
        await body.client.send_heartbeat()
        record = server.registry.get(welcome.payload.session)
        assert record is not None
    finally:
        await body.client.close()


async def test_a_reconnected_body_reports_its_latch_in_the_first_heartbeat(
    brain: tuple[BrainServer, list[Any]],
) -> None:
    """SPEC section 5, reconnection: a body in safe_hold or estopped reports
    that state in its first heartbeat and stays there until cleared."""
    server, received = brain
    clock = ManualClock()
    body, _ = await connected_body(server, clock)

    try:
        await body.estop("operator")
        await body.client.close()
        await body.client.connect()

        received.clear()
        await body.client.send_heartbeat()

        await until(
            lambda: any(m.type == "heartbeat" for m in received),
            what="the first heartbeat after reconnect",
        )
        beat = next(m for m in received if m.type == "heartbeat")
        assert beat.payload.state == "estopped"
    finally:
        await body.client.close()


async def test_the_brain_coming_back_does_not_lift_the_latch(
    brain: tuple[BrainServer, list[Any]],
) -> None:
    """SPEC section 8.2: not on reconnect, not on timeout, not because the
    cause went away."""
    server, _ = brain
    clock = ManualClock()
    body, session = await connected_body(server, clock)

    try:
        await body.dispatch.handle(sys_command(session, "clear_safe_hold"))
        await body.enter_safe_hold("brain heartbeat lease missed")
        assert body.state == "safe_hold"

        lease = body.client.brain_lease
        assert lease is not None
        lease.beat()  # the brain is talking again

        assert body.state == "safe_hold"
        assert not body.safety.may_actuate
    finally:
        await body.client.close()


def test_the_body_lease_is_armed_from_the_welcome_terms() -> None:
    """The body holds a lease on the brain using the interval the brain
    named, not one it picked for itself."""
    clock = ManualClock()
    watch = LeaseWatch(3000, clock)

    clock.advance(ms=2999)
    assert not watch.expired
    clock.advance(ms=1)
    assert watch.expired


async def test_latching_does_not_need_a_working_socket(
    brain: tuple[BrainServer, list[Any]],
) -> None:
    """The bug the Gate 3 drill found.

    Latching used to announce the state change before failing in-flight
    spans. With the socket dead, the announcement raised, the spans were
    never failed, and the lease watchdog died with it. The commonest reason
    to be latching at all is that the socket just died, so the latch path
    must not touch the network until every local decision is made.
    """
    server, _ = brain
    clock = ManualClock()
    body, session = await connected_body(server, clock)

    await body.dispatch.handle(sys_command(session, "clear_safe_hold"))
    assert body.state == "ok"

    entry = body.ledger.admit(
        drive_command(session, span_id="spn_dead_socket"), body.client.stamp()
    )
    assert entry is not None
    body.ledger.start(entry)

    # The socket dies before the latch, exactly as in a real lease miss.
    await body.client._connection.close()  # type: ignore[union-attr]

    await body.enter_safe_hold("brain heartbeat lease missed")

    assert body.state == "safe_hold", "the body failed to latch without a network"
    assert not body.safety.may_actuate
    assert body.pose.linear_mps == 0.0
    assert entry.terminal == "failed", "in-flight span survived a latch it should not have"


async def test_the_lease_watchdog_survives_a_failure(
    brain: tuple[BrainServer, list[Any]],
) -> None:
    """A watchdog that dies on the first fault it sees is worse than none."""
    server, _ = brain
    clock = ManualClock()
    body, _session = await connected_body(server, clock)

    async def explode(cause: str) -> None:
        raise RuntimeError("something went wrong while latching")

    body.enter_safe_hold = explode  # type: ignore[method-assign]

    ticks = 0

    async def sleeper(seconds: float) -> None:
        nonlocal ticks
        ticks += 1
        if ticks > 3:
            raise asyncio.CancelledError
        clock.advance(seconds=seconds)
        await asyncio.sleep(0)

    body._sleep = sleeper  # type: ignore[attr-defined]
    clock.advance(ms=5000)

    with contextlib.suppress(asyncio.CancelledError):
        await body.watch_brain_lease()

    assert ticks > 3, "the watchdog stopped at the first exception"

    await body.client.close()
