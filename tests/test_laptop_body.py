"""The laptop body's camera (checklist step 4.1, SPEC sections 6.5 and 7.2).

Done-check: a snapshot round trip lands in MCAP and decodes to a valid image.

Everything here runs against a stubbed capture source. There is exactly one
live-camera test, marked `camera` and skipped unless asked for, because on
macOS the first real capture from a process raises a TCC permission dialog
and an unanswered dialog looks identical to a hang. CI has no camera and
nobody to click it.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from mcap.reader import make_reader
from PIL import Image

from bodies.camera import (
    CameraDependencyError,
    CameraPermissionError,
    CameraUnavailableError,
    Frame,
    StubCamera,
    stub_frame,
)
from bodies.client import BodyConfig
from bodies.laptop import CAMERA_ID, LaptopBody, laptop_manifest
from brain.config import ServerConfig
from brain.recorder import FlightRecorder
from brain.server import BrainServer
from wire import CommandEnvelope, CommandPayload, without_none
from wire.stamp import new_id, now

TOKEN = "laptop-token"

LIVE_CAMERA = os.environ.get("BRAIN_CAMERA_TESTS") == "1"


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


async def until(condition, *, what: str, spins: int = 40_000) -> None:
    for _ in range(spins):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"timed out waiting for {what}")


def snapshot_command(session: str, span_id: str = "spn_snap") -> CommandEnvelope:
    return CommandEnvelope(
        **without_none(
            type="command",
            id=new_id(),
            session=session,
            seq=2,
            ts=now(),
            trace_id="trc_look",
            span_id=span_id,
            payload=CommandPayload(
                capability=CAMERA_ID,
                action="snapshot",
                params={},
                ttl_ms=5000,
            ),
        )
    )


# The manifest


def test_a_sensor_only_body_may_boot_ok() -> None:
    """SPEC section 7.1. The worst a camera does is take a picture, and a
    hold that protects nothing would be cleared by rote every session."""
    assert laptop_manifest().boot_state == "ok"
    assert laptop_manifest().hardware_class == "workstation"


def test_the_camera_declares_only_what_it_does() -> None:
    """Streaming is in the class registry and not in this body. A subset is
    allowed; a promise the body cannot keep is not."""
    camera = next(c for c in laptop_manifest().capabilities if c.id == CAMERA_ID)

    assert camera.capability_class == "camera"
    assert camera.actions == ["snapshot"]
    assert camera.events == ["frame"]


def test_the_camera_declares_its_format_and_size() -> None:
    camera = next(c for c in laptop_manifest().capabilities if c.id == CAMERA_ID)
    assert camera.attributes is not None
    assert camera.attributes["formats"] == ["jpeg"]
    assert camera.attributes["resolutions"] == ["1280x720"]


# The capture source


def test_the_stub_returns_a_real_jpeg() -> None:
    """Not a placeholder byte string: something PIL will actually open."""
    frame = stub_frame()
    image = Image.open(io.BytesIO(frame.jpeg))

    assert image.format == "JPEG"
    assert image.size == (frame.width, frame.height)


def test_the_stub_is_deterministic() -> None:
    """A camera that invented new pixels each run would make ADR-0005's
    identical-replay promise untestable."""
    assert stub_frame().jpeg == stub_frame().jpeg


def test_frame_event_data_matches_the_spec_shape() -> None:
    """SPEC section 6.5."""
    data = stub_frame().as_event_data()

    assert set(data) == {"format", "b64", "width", "height"}
    assert data["format"] == "jpeg"
    assert base64.b64decode(data["b64"])  # type: ignore[arg-type]


def test_capturing_before_opening_is_an_error() -> None:
    with pytest.raises(CameraUnavailableError):
        StubCamera().capture()


# Snapshot, end to end


async def test_snapshot_emits_a_frame_event(
    brain: tuple[BrainServer, list[Any]],
) -> None:
    server, received = brain
    body = LaptopBody(config_for(server), camera=StubCamera())
    welcome = await body.client.connect()

    try:
        span = await body.dispatch.handle(snapshot_command(welcome.payload.session))
        assert span is not None
        assert span.terminal == "succeeded"

        await until(
            lambda: any(m.type == "event" and m.payload.event == "frame" for m in received),
            what="the frame event",
        )
        frame = next(m for m in received if m.type == "event" and m.payload.event == "frame")
        assert frame.payload.capability == CAMERA_ID
        assert frame.payload.data["format"] == "jpeg"
    finally:
        await body.client.close()


async def test_a_requested_snapshot_is_not_droppable(
    brain: tuple[BrainServer, list[Any]],
) -> None:
    """Streamed frames are droppable because another follows. A snapshot was
    asked for, and dropping it answers a question with silence."""
    server, received = brain
    body = LaptopBody(config_for(server), camera=StubCamera())
    welcome = await body.client.connect()

    try:
        await body.dispatch.handle(snapshot_command(welcome.payload.session))
        await until(
            lambda: any(m.type == "event" and m.payload.event == "frame" for m in received),
            what="the frame event",
        )
        frame = next(m for m in received if m.type == "event" and m.payload.event == "frame")
        assert frame.payload.droppable is False
    finally:
        await body.client.close()


async def test_the_frame_carries_the_commands_trace(
    brain: tuple[BrainServer, list[Any]],
) -> None:
    """ADR-0005 point 3: the picture and the reason for it are one thread."""
    server, received = brain
    body = LaptopBody(config_for(server), camera=StubCamera())
    welcome = await body.client.connect()

    try:
        await body.dispatch.handle(snapshot_command(welcome.payload.session))
        await until(
            lambda: any(m.type == "event" and m.payload.event == "frame" for m in received),
            what="the frame event",
        )
        frame = next(m for m in received if m.type == "event" and m.payload.event == "frame")
        assert frame.trace_id == "trc_look"
    finally:
        await body.client.close()


async def test_the_result_reports_the_shape_not_the_image(
    brain: tuple[BrainServer, list[Any]],
) -> None:
    """The base64 is the largest thing v1 puts on the wire; sending it twice
    would double the cost of a snapshot for nothing."""
    server, received = brain
    body = LaptopBody(config_for(server), camera=StubCamera())
    welcome = await body.client.connect()

    try:
        await body.dispatch.handle(snapshot_command(welcome.payload.session))
        await until(
            lambda: any(m.type == "command_result" for m in received),
            what="the result",
        )
        result = next(m for m in received if m.type == "command_result")

        assert result.payload.status == "succeeded"
        assert result.payload.data is not None
        assert result.payload.data["format"] == "jpeg"
        assert result.payload.data["width"] == 16
        assert "b64" not in result.payload.data
    finally:
        await body.client.close()


async def test_the_camera_is_opened_once_across_snapshots(
    brain: tuple[BrainServer, list[Any]],
) -> None:
    """Reopening per shot would put a device negotiation in front of every
    picture, and on macOS a permission check as well."""
    server, _received = brain
    camera = StubCamera()
    body = LaptopBody(config_for(server), camera=camera)
    welcome = await body.client.connect()

    try:
        for index in range(3):
            await body.dispatch.handle(
                snapshot_command(welcome.payload.session, span_id=f"spn_{index}")
            )
        assert camera.captures == 3
    finally:
        await body.client.close()


# The done-check


async def test_a_snapshot_lands_in_mcap_and_decodes_to_an_image(
    tmp_path: Path,
) -> None:
    """The step 4.1 done-check, all the way through.

    Command out, frame back, into the flight recorder, read off disk, decoded
    by an image library that knows nothing about this project.
    """
    path = tmp_path / "snapshot.mcap"
    recorder = FlightRecorder(path)
    recorder.open()

    config = ServerConfig(host="127.0.0.1", port=0, auth_token=TOKEN)
    async with BrainServer(config, recorder=recorder) as server:
        body = LaptopBody(config_for(server), camera=StubCamera())
        welcome = await body.client.connect()
        session = welcome.payload.session
        loops = asyncio.create_task(body.client.run_loops())

        command = snapshot_command(session)
        state = server.sessions[session]
        command.seq = state.record.outbound.take()
        server.registry.open_span(session, command)
        await state.send(command)

        await until(lambda: len(body.ledger) >= 1, what="the snapshot to be handled")
        await asyncio.sleep(0.05)
        loops.cancel()
        await body.client.close()

    recorder.close()

    with path.open("rb") as handle:
        records = [
            (channel.topic, json.loads(message.data))
            for _schema, channel, message in make_reader(handle).iter_messages()
        ]

    # The command that caused it is in the record too, not just the effect.
    assert any(topic == "/command" for topic, _ in records)

    frames = [
        record
        for topic, record in records
        if topic == "/event" and record["message"]["payload"]["event"] == "frame"
    ]
    assert len(frames) == 1, "the snapshot did not reach the recording"

    data = frames[0]["message"]["payload"]["data"]
    image = Image.open(io.BytesIO(base64.b64decode(data["b64"])))
    image.load()  # actually decode the pixels, not just the header

    assert image.format == "JPEG"
    assert image.size == (data["width"], data["height"])


# Failure, named rather than hung


async def test_a_camera_failure_is_reported_not_swallowed(
    brain: tuple[BrainServer, list[Any]],
) -> None:
    server, _received = brain

    class RefusingCamera(StubCamera):
        def open(self) -> None:
            raise CameraPermissionError("camera 0 would not produce a frame. On macOS ...")

    body = LaptopBody(config_for(server), camera=RefusingCamera())
    welcome = await body.client.connect()

    try:
        span = await body.dispatch.handle(snapshot_command(welcome.payload.session))
        assert span is not None
        assert span.terminal == "failed"
    finally:
        await body.client.close()


def test_the_denial_message_names_the_permission() -> None:
    """A hang tells an operator nothing. This has to say what to go and do."""
    from bodies.camera import _denied_message

    message = _denied_message(0)

    assert "permission" in message.lower()
    assert "Privacy & Security" in message
    assert "runbook" in message


def test_a_missing_dependency_says_how_to_install_it() -> None:
    from bodies.camera import CameraDependencyError, _load_cv2

    try:
        _load_cv2()
    except CameraDependencyError as exc:
        assert "uv sync --extra laptop" in str(exc)
    except ImportError:  # pragma: no cover
        pytest.fail("_load_cv2 must raise CameraDependencyError, not ImportError")


def test_camera_errors_share_a_base() -> None:
    """So a caller can catch one thing and still tell them apart in a log."""
    from bodies.camera import CameraError

    for error in (CameraPermissionError, CameraUnavailableError, CameraDependencyError):
        assert issubclass(error, CameraError)


# Local only


@pytest.mark.camera
@pytest.mark.skipif(not LIVE_CAMERA, reason="set BRAIN_CAMERA_TESTS=1 to use a real camera")
async def test_a_real_camera_produces_a_decodable_frame() -> None:
    """The only test that touches hardware.

    Skipped unless asked for. The first run from a given process raises the
    macOS permission dialog; grant it once and it is remembered. See
    docs/runbook-laptop-body.md.
    """
    from bodies.camera import OpenCVCamera

    camera = OpenCVCamera()
    camera.open()
    try:
        frame = camera.capture()
    finally:
        camera.close()

    image = Image.open(io.BytesIO(frame.jpeg))
    image.load()

    assert image.format == "JPEG"
    assert frame.width > 0 and frame.height > 0
    assert isinstance(frame, Frame)
