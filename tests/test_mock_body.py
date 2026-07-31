"""The mock body (checklist step 3.1, SPEC section 7, ADR-0003).

Done-check: handshake completes, manifest validates, and a `sys` state event
is emitted on boot.

The body is driven against a real `BrainServer` over a real socket, because
the thing being tested is that two processes agree on a contract. Timers run
on an injected clock so nothing here waits.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from bodies.client import BodyClient, BodyConfig, HandshakeRejected
from bodies.mock import (
    MAX_ANGULAR_RPS,
    MAX_LINEAR_MPS,
    MockBody,
    Pose,
    config_from_env,
    mock_manifest,
)
from brain.config import ServerConfig
from brain.server import BrainServer
from helpers import ticker
from wire import Manifest, capability_classes, is_valid, to_object
from wire.clock import ManualClock
from wire.schema import protocol_version

TOKEN = "test-token"


@pytest.fixture
async def brain() -> AsyncIterator[tuple[BrainServer, list[Any]]]:
    """A running brain plus everything it received."""
    received: list[Any] = []

    async def collect(state: Any, reception: Any) -> None:
        received.append(reception.message)

    config = ServerConfig(host="127.0.0.1", port=0, auth_token=TOKEN)
    async with BrainServer(config, on_message=collect) as server:
        yield server, received


def body_config(server: BrainServer, *, token: str = TOKEN) -> BodyConfig:
    return BodyConfig(url=f"ws://127.0.0.1:{server.port}", auth_token=token)


async def until(condition, *, what: str, spins: int = 20_000) -> None:
    for _ in range(spins):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"timed out waiting for {what}")


# The manifest, before anything connects


def test_the_manifest_is_a_legal_manifest() -> None:
    """It has to survive the same validation as anything on the wire."""
    manifest = mock_manifest()
    rendered = manifest.model_dump(by_alias=True, exclude_unset=True, mode="json")

    assert rendered["body_id"] == "mock-01"
    assert rendered["hardware_class"] == "virtual"
    assert Manifest.model_validate(rendered) == manifest


def test_the_manifest_declares_sys() -> None:
    """SPEC section 7.1: every manifest MUST include the system capability
    with id `sys`."""
    ids = [capability.id for capability in mock_manifest().capabilities]
    assert "sys" in ids


def test_the_manifest_declares_the_capabilities_the_checklist_asks_for() -> None:
    classes = {capability.capability_class for capability in mock_manifest().capabilities}
    assert classes == {"system", "differential_drive", "range_sensor"}


def test_every_declared_class_is_in_the_registry() -> None:
    """The registry is in the schema, so this cannot drift from it."""
    for capability in mock_manifest().capabilities:
        assert capability.capability_class in capability_classes()


def test_a_body_with_actuation_boots_into_safe_hold() -> None:
    """SPEC section 7.1. Stopped is the default and motion is permissioned
    (section 8.4), so a drive that booted `ok` would be able to move before
    anyone said it could."""
    assert mock_manifest().boot_state == "safe_hold"


def test_the_drive_declares_bounds_for_the_validator() -> None:
    """ADR-0004 grounds `set_velocity` against these."""
    drive = next(c for c in mock_manifest().capabilities if c.id == "drive0")
    assert drive.attributes == {
        "max_linear_mps": MAX_LINEAR_MPS,
        "max_angular_rps": MAX_ANGULAR_RPS,
    }


def test_body_id_is_configurable() -> None:
    assert mock_manifest("mock-02").body_id == "mock-02"


# The handshake


async def test_handshake_completes(brain: tuple[BrainServer, list[Any]]) -> None:
    """The done-check: a real body against a real brain."""
    server, _ = brain
    client = BodyClient(mock_manifest(), body_config(server))

    welcome = await client.connect()
    try:
        assert welcome.type == "welcome"
        assert client.connected
        assert client.session == welcome.payload.session
        assert client.protocol_version == protocol_version()
        assert client.heartbeat_interval_ms == 1000
        assert client.heartbeat_lease_ms == 3000
    finally:
        await client.close()


async def test_the_brain_registers_the_body_and_its_manifest(
    brain: tuple[BrainServer, list[Any]],
) -> None:
    server, _ = brain
    client = BodyClient(mock_manifest(), body_config(server))

    welcome = await client.connect()
    try:
        record = server.registry.get(welcome.payload.session)
        assert record is not None
        assert record.body_id == "mock-01"
        assert [c.id for c in record.manifest.capabilities] == ["sys", "drive0", "range0"]
        assert record.manifest.boot_state == "safe_hold"
    finally:
        await client.close()


async def test_the_hello_is_a_legal_message(brain: tuple[BrainServer, list[Any]]) -> None:
    """If the manifest were malformed the brain would reject it, so a
    completed handshake is itself the validation. This checks the bytes."""
    server, _ = brain
    client = BodyClient(mock_manifest(), body_config(server))
    hello = client._hello()  # the exact message connect() sends

    assert is_valid(to_object(hello))


async def test_a_bad_token_is_refused(brain: tuple[BrainServer, list[Any]]) -> None:
    server, _ = brain
    client = BodyClient(mock_manifest(), body_config(server, token="wrong"))

    with pytest.raises(HandshakeRejected) as caught:
        await client.connect()

    assert caught.value.code == "auth_failed"
    assert not client.connected


async def test_an_unsupported_version_is_refused(brain: tuple[BrainServer, list[Any]]) -> None:
    server, _ = brain
    config = BodyConfig(
        url=f"ws://127.0.0.1:{server.port}",
        auth_token=TOKEN,
        protocol_versions=("2099-01-01",),
    )
    client = BodyClient(mock_manifest(), config)

    with pytest.raises(HandshakeRejected) as caught:
        await client.connect()

    assert caught.value.code == "unsupported_version"
    assert caught.value.supported == [protocol_version()]


# Boot state announcement


async def test_a_sys_state_event_is_emitted_on_boot(
    brain: tuple[BrainServer, list[Any]],
) -> None:
    """The done-check. SPEC section 6.5 reserves this event on every body and
    section 7.1 says an actuating body boots into safe_hold; without the
    announcement the brain would be guessing until something else revealed
    the state."""
    server, received = brain
    client = BodyClient(mock_manifest(), body_config(server))

    await client.connect()
    try:
        await client.announce_boot_state()
        await until(lambda: bool(received), what="the boot state event")

        event = received[0]
        assert event.type == "event"
        assert event.payload.capability == "sys"
        assert event.payload.event == "state"
        assert event.payload.data == {"state": "safe_hold", "cause": "boot"}
    finally:
        await client.close()


async def test_the_boot_state_event_is_not_droppable(
    brain: tuple[BrainServer, list[Any]],
) -> None:
    """A state change is not telemetry. SPEC section 6.5: `droppable` marks
    what the sender MAY skip under backpressure, and a body silently dropping
    news that it stopped is precisely the wrong thing to lose."""
    server, received = brain
    client = BodyClient(mock_manifest(), body_config(server))

    await client.connect()
    try:
        await client.announce_boot_state()
        await until(lambda: bool(received), what="the boot state event")
        assert received[0].payload.droppable is False
    finally:
        await client.close()


async def test_state_transitions_are_announced(brain: tuple[BrainServer, list[Any]]) -> None:
    server, received = brain
    client = BodyClient(mock_manifest(), body_config(server))

    await client.connect()
    try:
        await client.announce_boot_state()
        await client.emit_state("ok", cause="clear_safe_hold")
        await until(lambda: len(received) >= 2, what="both state events")

        assert client.state == "ok"
        assert received[1].payload.data == {"state": "ok", "cause": "clear_safe_hold"}
    finally:
        await client.close()


# Telemetry


async def test_telemetry_reports_odometry_and_range(
    brain: tuple[BrainServer, list[Any]],
) -> None:
    server, received = brain
    clock = ManualClock()
    body = MockBody(body_config(server), clock=clock, sleep=ticker(clock, 0, park=True))

    await body.client.connect()
    try:
        await body.emit_telemetry(0.5)
        await until(lambda: len(received) >= 2, what="odometry and range")

        odometry, ranging = received[0], received[1]
        assert odometry.payload.capability == "drive0"
        assert odometry.payload.event == "odometry"
        assert ranging.payload.capability == "range0"
        assert ranging.payload.event == "range"
    finally:
        await body.client.close()


async def test_telemetry_is_droppable(brain: tuple[BrainServer, list[Any]]) -> None:
    """SPEC section 6.5: sensor readings are what backpressure may skip."""
    server, received = brain
    clock = ManualClock()
    body = MockBody(body_config(server), clock=clock, sleep=ticker(clock, 0, park=True))

    await body.client.connect()
    try:
        await body.emit_telemetry(0.5)
        await until(lambda: len(received) >= 2, what="telemetry")
        assert all(message.payload.droppable for message in received[:2])
    finally:
        await body.client.close()


def test_range_readings_are_deterministic() -> None:
    """ADR-0005 promises a replayed session produces identical decisions. A
    random sensor would make that untestable, so the fake sweeps a fixed
    pattern instead."""
    first = MockBody(BodyConfig(url="ws://unused"))
    second = MockBody(BodyConfig(url="ws://unused"))

    readings: list[tuple[float, float]] = []
    for _ in range(25):
        first._ticks += 1
        second._ticks += 1
        readings.append((first._fake_range(), second._fake_range()))

    assert all(a == b for a, b in readings)
    assert len({a for a, _ in readings}) > 1, "a constant is not a sweep"


def test_range_readings_stay_inside_the_declared_bounds() -> None:
    """A sensor reporting outside what its manifest declares would make the
    manifest a lie, and the manifest is what the planner grounds against."""
    body = MockBody(BodyConfig(url="ws://unused"))
    sensor = next(c for c in mock_manifest().capabilities if c.id == "range0")
    low, high = sensor.attributes["min_m"], sensor.attributes["max_m"]  # type: ignore[index]

    for _ in range(60):
        body._ticks += 1
        assert low <= body._fake_range() <= high


# Dead reckoning


def test_a_stopped_pose_does_not_move() -> None:
    pose = Pose()
    pose.integrate(1.0)
    assert (pose.x_m, pose.y_m, pose.heading_rad) == (0.0, 0.0, 0.0)


def test_driving_forward_moves_the_pose() -> None:
    pose = Pose(linear_mps=0.2)
    pose.integrate(1.0)
    assert pose.x_m == pytest.approx(0.2)


def test_stop_clears_both_velocities() -> None:
    pose = Pose(linear_mps=0.3, angular_rps=0.5)
    pose.stop()
    assert (pose.linear_mps, pose.angular_rps) == (0.0, 0.0)


# Configuration


def test_the_brain_url_is_body_side_config() -> None:
    """SPEC section 3.1: bodies connect to the brain, and the brain never
    dials out, so the URL can only live here."""
    config = config_from_env({"BRAIN_URL": "ws://elsewhere:9999", "BRAIN_AUTH_TOKEN": "s"})
    assert config.url == "ws://elsewhere:9999"
    assert config.auth_token == "s"


def test_config_defaults_to_localhost() -> None:
    assert config_from_env({}).url == "ws://127.0.0.1:8765"


def test_the_body_offers_the_version_it_speaks() -> None:
    assert config_from_env({}).protocol_versions == (protocol_version(),)
