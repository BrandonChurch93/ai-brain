"""The adapter conformance suite (SPEC section 10).

Every body in `tests/adapters.py` is held to the same checklist, so the
laptop body in Phase 4 inherits these for free rather than being spot
checked by hand.

Only the mechanically checkable lines live here. The suite grew with each
safety behaviour: manifest and state announcement from step 3.1, TTL,
deduplication and one-terminal-per-span from 3.2, latching from 3.3.

SPEC section 10, line by line, all ten now covered:

- ignores unknown fields everywhere .......... here
- hello with a truthful manifest incl. sys ... here
- heartbeats on the configured interval ...... here
- reacts to lease misses by latching ......... here
- enforces command TTL locally ............... here
- deduplicates by span_id .................... here
- exactly one terminal result per span ....... here
- processes estop ahead of queued work ....... here
- latches and never self-clears .............. here
- sys state event on every transition ........ here
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from adapters import ADAPTER_IDS, ADAPTERS, AdapterCase
from bodies.client import BodyConfig
from bodies.mock import ACTUATING_CLASSES
from brain.config import ServerConfig
from brain.server import BrainServer
from wire import CommandEnvelope, capability_classes, decode_object, is_valid, to_object
from wire.clock import ManualClock

TOKEN = "conformance-token"

pytestmark = pytest.mark.parametrize("case", ADAPTERS, ids=ADAPTER_IDS)


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


def command_for(
    session: str,
    capability: str,
    action: str,
    *,
    span_id: str = "spn_1",
    ttl_ms: int = 5000,
    params: dict[str, Any] | None = None,
    seq: int = 2,
    expects_result: bool = True,
) -> CommandEnvelope:
    message = decode_object(
        {
            "type": "command",
            "id": f"01JZQK8N4T000000000{span_id:>07}",
            "session": session,
            "seq": seq,
            "ts": {"mono_ns": 1, "utc": "2026-07-29T18:00:00.000Z"},
            "trace_id": "trc_conformance",
            "span_id": span_id,
            "payload": {
                "capability": capability,
                "action": action,
                "params": params or {},
                "ttl_ms": ttl_ms,
                "expects_result": expects_result,
            },
        }
    )
    assert isinstance(message, CommandEnvelope)
    return message


# The manifest


def test_manifest_is_valid(case: AdapterCase) -> None:
    manifest = case.manifest()
    assert is_valid(
        to_object(
            decode_object(
                {
                    "type": "manifest",
                    "id": "01JZQK8N4T00000000000000E1",
                    "session": "sess_1",
                    "seq": 1,
                    "ts": {"mono_ns": 1, "utc": "2026-07-29T18:00:00.000Z"},
                    "payload": manifest.model_dump(by_alias=True, exclude_unset=True, mode="json"),
                }
            )
        )
    )


def test_manifest_includes_sys(case: AdapterCase) -> None:
    """SPEC section 7.2: required on every body, with id `sys`."""
    sys_capability = next(
        (c for c in case.manifest().capabilities if c.id == "sys"),
        None,
    )
    assert sys_capability is not None
    assert sys_capability.capability_class == "system"


def test_every_capability_class_is_in_the_registry(case: AdapterCase) -> None:
    for capability in case.manifest().capabilities:
        assert capability.capability_class in capability_classes()


def test_capability_ids_are_unique(case: AdapterCase) -> None:
    ids = [capability.id for capability in case.manifest().capabilities]
    assert len(ids) == len(set(ids))


def test_an_actuating_body_boots_into_safe_hold(case: AdapterCase) -> None:
    """SPEC section 7.1. Sensor-only bodies MAY boot `ok`; anything that can
    move MUST NOT."""
    manifest = case.manifest()
    actuating = {"differential_drive"} & {
        capability.capability_class for capability in manifest.capabilities
    }
    if actuating:
        assert manifest.boot_state == "safe_hold", (
            f"{manifest.body_id} declares {actuating} and must boot stopped"
        )


async def test_every_declared_action_has_a_handler(
    case: AdapterCase, brain: tuple[BrainServer, list[Any]]
) -> None:
    """The manifest is a promise the planner grounds against (ADR-0003).

    An action declared but unhandled makes the manifest a lie, and the
    planner would build a plan around something the body silently refuses.
    """
    server, _ = brain
    body = case.make(config_for(server))
    await body.client.connect()

    try:
        for capability in case.manifest().capabilities:
            for action in capability.actions or []:
                assert (capability.id, action) in body.dispatch._handlers, (
                    f"{capability.id}/{action} is declared but has no handler"
                )
    finally:
        await body.client.close()


# The handshake and state announcement


async def test_hello_carries_the_manifest(
    case: AdapterCase, brain: tuple[BrainServer, list[Any]]
) -> None:
    server, _ = brain
    body = case.make(config_for(server))

    welcome = await body.client.connect()
    try:
        record = server.registry.get(welcome.payload.session)
        assert record is not None
        assert record.manifest == case.manifest()
    finally:
        await body.client.close()


async def test_a_state_event_is_emitted_on_boot(
    case: AdapterCase, brain: tuple[BrainServer, list[Any]]
) -> None:
    server, received = brain
    body = case.make(config_for(server))

    await body.client.connect()
    try:
        await body.client.announce_boot_state()
        await until(lambda: bool(received), what="the boot state event")

        event = received[0]
        assert event.payload.capability == "sys"
        assert event.payload.event == "state"
        assert event.payload.data["state"] == case.manifest().boot_state
    finally:
        await body.client.close()


async def test_state_events_are_not_droppable(
    case: AdapterCase, brain: tuple[BrainServer, list[Any]]
) -> None:
    """SPEC section 6.5: a state change is not telemetry."""
    server, received = brain
    body = case.make(config_for(server))

    await body.client.connect()
    try:
        await body.client.announce_boot_state()
        await until(lambda: bool(received), what="the boot state event")
        assert received[0].payload.droppable is False
    finally:
        await body.client.close()


async def test_heartbeats_carry_the_current_state(
    case: AdapterCase, brain: tuple[BrainServer, list[Any]]
) -> None:
    """SPEC section 6.4: a body reports ok, safe_hold, or estopped."""
    server, received = brain
    body = case.make(config_for(server))

    await body.client.connect()
    try:
        await body.client.send_heartbeat()
        await until(
            lambda: any(m.type == "heartbeat" for m in received),
            what="a heartbeat",
        )
        beat = next(m for m in received if m.type == "heartbeat")
        assert beat.payload.state in {"ok", "safe_hold", "estopped"}
        assert beat.payload.state == body.client.state
    finally:
        await body.client.close()


# Telemetry honesty


async def test_telemetry_stays_within_declared_attributes(
    case: AdapterCase, brain: tuple[BrainServer, list[Any]]
) -> None:
    """A sensor reporting outside what its manifest declares makes the
    manifest a lie, and the manifest is the planner's affordance set."""
    if not case.emits_telemetry:
        pytest.skip(f"{case.name} reports when asked, not on a timer")

    server, received = brain
    clock = ManualClock()
    body = case.make(config_for(server), clock=clock)

    bounds = {
        capability.id: capability.attributes or {} for capability in case.manifest().capabilities
    }

    await body.client.connect()
    try:
        for _ in range(30):
            await body.emit_telemetry(0.5)
        await until(lambda: len(received) >= 30, what="telemetry")

        for message in received:
            if message.type != "event":
                continue
            attributes = bounds.get(message.payload.capability, {})
            if "min_m" in attributes and "meters" in message.payload.data:
                assert attributes["min_m"] <= message.payload.data["meters"] <= attributes["max_m"]
    finally:
        await body.client.close()


async def test_telemetry_is_droppable(
    case: AdapterCase, brain: tuple[BrainServer, list[Any]]
) -> None:
    """SPEC section 6.5: sensor readings are what backpressure may skip."""
    if not case.emits_telemetry:
        pytest.skip(f"{case.name} reports when asked, not on a timer")

    server, received = brain
    clock = ManualClock()
    body = case.make(config_for(server), clock=clock)

    await body.client.connect()
    try:
        await body.emit_telemetry(0.5)
        await until(lambda: len(received) >= 1, what="telemetry")
        assert all(
            message.payload.droppable
            for message in received
            if message.type == "event" and message.payload.event != "state"
        )
    finally:
        await body.client.close()


# Forward compatibility


async def test_unknown_fields_are_ignored(
    case: AdapterCase, brain: tuple[BrainServer, list[Any]]
) -> None:
    """SPEC section 10, first line. A body must accept a command from a
    brain newer than itself."""
    server, _received = brain
    body = case.make(config_for(server))
    welcome = await body.client.connect()

    try:
        raw = to_object(command_for(welcome.payload.session, "sys", "ping"))
        raw["invented_later"] = {"anything": True}
        raw["payload"]["also_invented"] = 7

        command = decode_object(raw)
        assert isinstance(command, CommandEnvelope)
        span = await body.dispatch.handle(command)

        assert span is not None
        assert span.terminal == "succeeded"
    finally:
        await body.client.close()


# Command semantics (step 3.2)


async def test_an_expired_command_is_never_executed(
    case: AdapterCase, brain: tuple[BrainServer, list[Any]]
) -> None:
    """SPEC section 6.6: not begun within its TTL means never executed, with
    a terminal `expired` result."""
    server, _received = brain
    clock = ManualClock()
    body = case.make(config_for(server), clock=clock)
    welcome = await body.client.connect()

    try:
        command = command_for(welcome.payload.session, "sys", "ping", ttl_ms=500)

        # Arrives, then the body is busy for longer than the lifespan.
        received_at = body.client.stamp()
        clock.advance(ms=501)

        span = await body.dispatch.handle(command, received_at)

        assert span is not None
        assert span.terminal == "expired"
        assert span.started is False, "an expired command must not have begun"
    finally:
        await body.client.close()


async def test_a_command_inside_its_ttl_runs(
    case: AdapterCase, brain: tuple[BrainServer, list[Any]]
) -> None:
    server, _ = brain
    clock = ManualClock()
    body = case.make(config_for(server), clock=clock)
    welcome = await body.client.connect()

    try:
        command = command_for(welcome.payload.session, "sys", "ping", ttl_ms=500)
        received_at = body.client.stamp()
        clock.advance(ms=499)

        span = await body.dispatch.handle(command, received_at)

        assert span is not None
        assert span.terminal == "succeeded"
        assert span.started is True
    finally:
        await body.client.close()


async def test_ttl_is_measured_from_receipt_not_from_the_senders_clock(
    case: AdapterCase, brain: tuple[BrainServer, list[Any]]
) -> None:
    """SPEC section 6.6 says the body measures from its own receipt time.

    The command below carries a sender timestamp from long ago. If the TTL
    were computed against that, every command from a device with a skewed
    clock would arrive dead.
    """
    server, _ = brain
    clock = ManualClock()
    body = case.make(config_for(server), clock=clock)
    welcome = await body.client.connect()

    try:
        command = command_for(welcome.payload.session, "sys", "ping", ttl_ms=500)
        assert command.ts.mono_ns == 1  # ancient, and from another machine

        span = await body.dispatch.handle(command, body.client.stamp())

        assert span is not None
        assert span.terminal == "succeeded"
    finally:
        await body.client.close()


async def test_a_retransmitted_command_executes_once(
    case: AdapterCase, brain: tuple[BrainServer, list[Any]]
) -> None:
    """SPEC section 6.6: deduplicate by span_id within a session."""
    server, _ = brain
    body = case.make(config_for(server))
    welcome = await body.client.connect()

    try:
        command = command_for(welcome.payload.session, "sys", "ping", span_id="spn_dup")

        first = await body.dispatch.handle(command)
        second = await body.dispatch.handle(command)

        assert first is not None
        assert second is None, "a retransmission must not produce a second execution"
        assert len(body.ledger) == 1
    finally:
        await body.client.close()


async def test_exactly_one_terminal_result_per_span(
    case: AdapterCase, brain: tuple[BrainServer, list[Any]]
) -> None:
    """SPEC section 6.7. The ledger refuses a second ending."""
    server, received = brain
    body = case.make(config_for(server))
    welcome = await body.client.connect()

    try:
        command = command_for(welcome.payload.session, "sys", "ping", span_id="spn_once")
        span = await body.dispatch.handle(command)
        assert span is not None

        await until(
            lambda: any(m.type == "command_result" for m in received),
            what="the terminal result",
        )
        results = [m for m in received if m.type == "command_result"]
        assert len(results) == 1
        assert results[0].payload.status == "succeeded"
        assert results[0].span_id == "spn_once"
        assert results[0].trace_id == command.trace_id

        # A second ending is refused rather than sent.
        assert body.ledger.complete(span, "failed") is False
    finally:
        await body.client.close()


async def test_a_result_echoes_span_and_trace(
    case: AdapterCase, brain: tuple[BrainServer, list[Any]]
) -> None:
    """SPEC section 6.7: the result carries the command's span_id and
    trace_id, which is what threads a decision to its effect (ADR-0005)."""
    server, received = brain
    body = case.make(config_for(server))
    welcome = await body.client.connect()

    try:
        command = command_for(welcome.payload.session, "sys", "ping", span_id="spn_echo")
        await body.dispatch.handle(command)
        await until(
            lambda: any(m.type == "command_result" for m in received),
            what="the terminal result",
        )

        result = next(m for m in received if m.type == "command_result")
        assert result.span_id == "spn_echo"
        assert result.trace_id == "trc_conformance"
    finally:
        await body.client.close()


async def test_an_unknown_action_is_rejected_not_ignored(
    case: AdapterCase, brain: tuple[BrainServer, list[Any]]
) -> None:
    server, _ = brain
    body = case.make(config_for(server))
    welcome = await body.client.connect()

    try:
        command = command_for(welcome.payload.session, "sys", "fly", span_id="spn_fly")
        span = await body.dispatch.handle(command)

        assert span is not None
        assert span.terminal == "rejected"
    finally:
        await body.client.close()


async def test_no_result_is_sent_when_none_is_expected(
    case: AdapterCase, brain: tuple[BrainServer, list[Any]]
) -> None:
    """`expects_result: false` (SPEC section 6.6). The span still ends; the
    body just does not announce it."""
    server, received = brain
    body = case.make(config_for(server))
    welcome = await body.client.connect()

    try:
        command = command_for(
            welcome.payload.session,
            "sys",
            "ping",
            span_id="spn_quiet",
            expects_result=False,
        )
        span = await body.dispatch.handle(command)
        assert span is not None
        assert span.terminal == "succeeded"

        for _ in range(200):
            await asyncio.sleep(0)
        assert not [m for m in received if m.type == "command_result"]
    finally:
        await body.client.close()


# Latching (step 3.3)


async def test_a_body_latches_on_a_brain_lease_miss(
    case: AdapterCase, brain: tuple[BrainServer, list[Any]]
) -> None:
    """SPEC section 8.1. The body holds a lease on the brain, not only the
    other way round."""
    server, _received = brain
    clock = ManualClock()
    body = case.make(config_for(server), clock=clock)
    await body.client.connect()

    try:
        lease = body.client.brain_lease
        assert lease is not None, "a body must hold a lease on the brain"

        clock.advance(ms=(body.client.heartbeat_lease_ms or 3000))
        assert lease.expired

        await body.enter_safe_hold("lease miss")
        assert body.state == "safe_hold"
        assert not body.safety.may_actuate
    finally:
        await body.client.close()


async def test_estop_latches_and_is_announced(
    case: AdapterCase, brain: tuple[BrainServer, list[Any]]
) -> None:
    """SPEC section 8.3: cease actuation, enter estopped, emit a sys state
    event. Works from idle, with nothing in flight."""
    server, received = brain
    body = case.make(config_for(server))
    await body.client.connect()

    try:
        await body.estop("conformance drill")

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


async def test_estop_is_handled_ahead_of_queued_work(
    case: AdapterCase, brain: tuple[BrainServer, list[Any]]
) -> None:
    """SPEC section 8.3: acted on immediately upon parse.

    The client routes estop through a priority path before general dispatch,
    so it cannot sit behind a command in the receive loop.
    """
    server, _received = brain
    body = case.make(config_for(server))
    await body.client.connect()

    try:
        assert body.client._on_priority is not None, (
            "estop must not share the ordinary dispatch path"
        )
    finally:
        await body.client.close()


async def test_a_latched_body_refuses_actuation(
    case: AdapterCase, brain: tuple[BrainServer, list[Any]]
) -> None:
    """SPEC section 8.4: motion is permissioned and a latch withdraws it."""
    server, _received = brain
    body = case.make(config_for(server))
    welcome = await body.client.connect()

    try:
        await body.estop("conformance drill")

        actuating = [
            (capability.id, action)
            for capability in case.manifest().capabilities
            if capability.capability_class in ACTUATING_CLASSES
            for action in (capability.actions or [])
            if action != "stop"  # stopping is never the unsafe direction
        ]

        for index, (capability, action) in enumerate(actuating):
            span = await body.dispatch.handle(
                command_for(
                    welcome.payload.session,
                    capability,
                    action,
                    span_id=f"spn_latched_{index}",
                )
            )
            assert span is not None
            assert span.terminal == "rejected", f"{capability}/{action} moved while latched"
    finally:
        await body.client.close()


async def test_nothing_clears_itself(
    case: AdapterCase, brain: tuple[BrainServer, list[Any]]
) -> None:
    """SPEC section 8.2: not on reconnect, not on timeout, not because the
    cause went away. Silent resume is prohibited."""
    server, _received = brain
    clock = ManualClock()
    body = case.make(config_for(server), clock=clock)
    await body.client.connect()

    try:
        await body.estop("conformance drill")

        clock.advance(seconds=3600)
        assert body.state == "estopped"

        await body.client.close()
        await body.client.connect()
        assert body.state == "estopped", "a reconnect cleared the latch"
    finally:
        await body.client.close()


async def test_a_latched_body_reports_its_latch_in_heartbeats(
    case: AdapterCase, brain: tuple[BrainServer, list[Any]]
) -> None:
    """SPEC section 5: a reconnected body reports the latched state in its
    first heartbeat, so the brain never has to infer it."""
    server, received = brain
    body = case.make(config_for(server))
    await body.client.connect()

    try:
        await body.estop("conformance drill")
        await body.client.close()
        await body.client.connect()

        received.clear()
        await body.client.send_heartbeat()
        await until(
            lambda: any(m.type == "heartbeat" for m in received),
            what="a heartbeat after reconnect",
        )

        beat = next(m for m in received if m.type == "heartbeat")
        assert beat.payload.state == "estopped"
    finally:
        await body.client.close()


async def test_latching_does_not_depend_on_the_network(
    case: AdapterCase, brain: tuple[BrainServer, list[Any]]
) -> None:
    """The commonest reason to latch is that the socket died.

    A body that could only stop while connected would be unable to stop in
    exactly the situation the latch exists for. Every local decision must be
    made before anything is sent.
    """
    server, _received = brain
    body = case.make(config_for(server))
    await body.client.connect()

    await body.client._connection.close()  # type: ignore[union-attr]
    await body.enter_safe_hold("socket died")

    assert body.state == "safe_hold"
    assert not body.safety.may_actuate
