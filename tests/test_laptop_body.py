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
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from mcap.reader import make_reader
from PIL import Image

from bodies.audio import (
    PCM_S16LE,
    AudioChunk,
    AudioFormat,
    StubMicrophone,
    tone,
)
from bodies.camera import (
    CameraPermissionError,
    CameraUnavailableError,
    Frame,
    FrameFormat,
    StubCamera,
    stub_frame,
)
from bodies.client import BodyConfig
from bodies.laptop import CAMERA_ID, MICROPHONE_ID, LaptopBody, laptop_manifest
from brain.config import ServerConfig
from brain.recorder import FlightRecorder
from brain.server import BrainServer
from wire import ACTUATING_CLASSES, CommandEnvelope, CommandPayload, without_none
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


def stub_frame_format() -> FrameFormat:
    frame = stub_frame()
    return FrameFormat(width=frame.width, height=frame.height)


def command_for(
    session: str,
    capability: str,
    action: str,
    span_id: str,
    ttl_ms: int = 5000,
) -> CommandEnvelope:
    return CommandEnvelope(
        **without_none(
            type="command",
            id=new_id(),
            session=session,
            seq=2,
            ts=now(),
            trace_id="trc_look",
            span_id=span_id,
            payload=CommandPayload(capability=capability, action=action, params={}, ttl_ms=ttl_ms),
        )
    )


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


def stub_body(server: BrainServer, **kwargs: Any) -> LaptopBody:
    """A laptop body with both devices stubbed."""
    kwargs.setdefault("camera", StubCamera())
    kwargs.setdefault("microphone", StubMicrophone())
    return LaptopBody(config_for(server), **kwargs)


def test_a_sensor_only_body_may_boot_ok() -> None:
    """SPEC section 7.1. Neither a camera nor a microphone moves anything,
    and a hold protecting nothing would be cleared by rote every session."""
    manifest = laptop_manifest(camera={"formats": ["jpeg"]})
    assert manifest.boot_state == "ok"
    assert manifest.hardware_class == "workstation"
    assert not ACTUATING_CLASSES & {c.capability_class for c in manifest.capabilities}


def test_the_camera_declares_only_what_it_does() -> None:
    """Streaming is in the class registry and not in this body. A subset is
    allowed; a promise the body cannot keep is not."""
    manifest = laptop_manifest(camera=stub_frame_format().as_attributes(5))
    camera = next(c for c in manifest.capabilities if c.id == CAMERA_ID)

    assert camera.capability_class == "camera"
    assert camera.actions == ["snapshot"]
    assert camera.events == ["frame"]


def test_a_device_that_did_not_open_is_not_declared() -> None:
    """A manifest naming what the body cannot deliver is worse than a
    smaller manifest: the planner grounds its plans on that promise."""
    manifest = laptop_manifest()
    ids = [c.id for c in manifest.capabilities]

    assert ids == ["sys"]
    assert CAMERA_ID not in ids
    assert MICROPHONE_ID not in ids


def test_the_manifest_reports_what_the_devices_actually_opened() -> None:
    """The reason attributes are passed in rather than assumed. This device
    hands back 48 kHz stereo when 16 kHz mono was asked for, which is exactly
    what real hardware does."""
    actual = AudioFormat(sample_rate_hz=48000, channels=2)
    manifest = laptop_manifest(microphone=actual.as_attributes())
    mic = next(c for c in manifest.capabilities if c.id == MICROPHONE_ID)

    assert mic.attributes == {
        "sample_rate_hz": 48000,
        "channels": 2,
        "encoding": "pcm_s16le",
    }


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
    body = stub_body(server)
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
    body = stub_body(server)
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
    body = stub_body(server)
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
    body = stub_body(server)
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
    body = stub_body(server, camera=camera)
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
        body = stub_body(server)
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
        def open(self) -> FrameFormat:
            raise CameraPermissionError("camera 0 would not start. On macOS ...")

    body = stub_body(server, camera=RefusingCamera())

    # It is not declared at all, so a planner never proposes a snapshot.
    assert not body.has_camera
    assert CAMERA_ID not in [c.id for c in body.client.manifest.capabilities]

    welcome = await body.client.connect()
    try:
        span = await body.dispatch.handle(snapshot_command(welcome.payload.session))
        assert span is not None
        assert span.terminal == "rejected"
    finally:
        await body.client.close()


async def test_a_camera_that_opens_then_fails_reports_failed(
    brain: tuple[BrainServer, list[Any]],
) -> None:
    """Different from never opening. The capability is real and declared; it
    is this one attempt that went wrong."""
    server, _received = brain

    class FlakyCamera(StubCamera):
        def capture(self) -> Frame:
            raise CameraUnavailableError("the driver stopped responding")

    body = stub_body(server, camera=FlakyCamera())
    assert body.has_camera

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


def test_device_errors_share_a_base_per_device() -> None:
    """So a caller can catch one thing and still tell them apart in a log."""
    from bodies.audio import (
        MicrophoneDependencyError,
        MicrophoneError,
        MicrophonePermissionError,
        MicrophoneUnavailableError,
    )
    from bodies.camera import CameraDependencyError, CameraError

    for error in (CameraPermissionError, CameraUnavailableError, CameraDependencyError):
        assert issubclass(error, CameraError)

    for error in (
        MicrophonePermissionError,
        MicrophoneUnavailableError,
        MicrophoneDependencyError,
    ):
        assert issubclass(error, MicrophoneError)


# Microphone (step 4.2)


def test_a_chunk_knows_its_own_format() -> None:
    """Every chunk carries the rate, not just the manifest. A recorded
    session is read long after the manifest scrolled past, and PCM whose
    rate you have to look up is PCM you can get wrong."""
    audio_format = AudioFormat(sample_rate_hz=16000, channels=1)
    chunk = AudioChunk(pcm=tone(audio_format, 0.25), format=audio_format)
    data = chunk.as_event_data()

    assert data["sample_rate_hz"] == 16000
    assert data["channels"] == 1
    assert data["encoding"] == PCM_S16LE
    assert data["frames"] == 4000
    assert data["duration_ms"] == pytest.approx(250)


def test_chunk_duration_follows_the_real_rate() -> None:
    """A quarter second is a quarter second at any rate, and the frame count
    is what changes. Computing it from a requested rate would misreport the
    length of everything a substituting device captured."""
    for rate in (16000, 44100, 48000):
        audio_format = AudioFormat(sample_rate_hz=rate, channels=1)
        chunk = AudioChunk(pcm=tone(audio_format, 0.25), format=audio_format)

        assert chunk.frames == rate // 4
        assert chunk.duration_ms == pytest.approx(250)


def test_the_stub_microphone_is_deterministic() -> None:
    first, second = StubMicrophone(), StubMicrophone()
    first.open()
    second.open()
    assert first.read(0.1).pcm == second.read(0.1).pcm


def test_the_stub_records_a_tone_not_silence() -> None:
    """Silence and a lost recording look identical. A tone does not."""
    microphone = StubMicrophone()
    microphone.open()
    assert set(microphone.read(0.1).pcm) != {0}


def test_reading_before_opening_is_an_error() -> None:
    from bodies.audio import MicrophoneUnavailableError

    with pytest.raises(MicrophoneUnavailableError):
        StubMicrophone().read(0.1)


async def test_start_capture_emits_audio_chunks(
    brain: tuple[BrainServer, list[Any]],
) -> None:
    server, received = brain
    body = stub_body(server, chunk_ms=20)
    welcome = await body.client.connect()

    try:
        span = await body.dispatch.handle(
            command_for(welcome.payload.session, MICROPHONE_ID, "start_capture", "spn_start")
        )
        assert span is not None
        assert span.terminal == "succeeded"
        assert body.capturing

        await until(
            lambda: len([m for m in received if _is_audio(m)]) >= 2,
            what="audio chunks",
        )
    finally:
        await body.stop_capture()
        await body.client.close()


async def test_the_start_result_reports_the_real_format(
    brain: tuple[BrainServer, list[Any]],
) -> None:
    """So the caller does not have to go back to the manifest to know what it
    is about to be sent."""
    server, received = brain
    body = stub_body(
        server,
        microphone=StubMicrophone(AudioFormat(sample_rate_hz=48000, channels=2)),
        chunk_ms=20,
    )
    welcome = await body.client.connect()

    try:
        await body.dispatch.handle(
            command_for(welcome.payload.session, MICROPHONE_ID, "start_capture", "spn_start")
        )
        await until(
            lambda: any(m.type == "command_result" for m in received),
            what="the result",
        )
        result = next(m for m in received if m.type == "command_result")
        assert result.payload.data["sample_rate_hz"] == 48000
        assert result.payload.data["channels"] == 2
    finally:
        await body.stop_capture()
        await body.client.close()


async def test_speech_is_not_droppable(brain: tuple[BrainServer, list[Any]]) -> None:
    """A dropped chunk is a hole in a sentence, and the next one does not
    replace it (SPEC section 6.5)."""
    server, received = brain
    body = stub_body(server, chunk_ms=20)
    welcome = await body.client.connect()

    try:
        await body.dispatch.handle(
            command_for(welcome.payload.session, MICROPHONE_ID, "start_capture", "spn_start")
        )
        await until(lambda: any(_is_audio(m) for m in received), what="an audio chunk")
        assert all(not m.payload.droppable for m in received if _is_audio(m))
    finally:
        await body.stop_capture()
        await body.client.close()


async def test_stop_capture_ends_it(brain: tuple[BrainServer, list[Any]]) -> None:
    server, received = brain
    body = stub_body(server, chunk_ms=20)
    welcome = await body.client.connect()

    try:
        await body.dispatch.handle(
            command_for(welcome.payload.session, MICROPHONE_ID, "start_capture", "spn_start")
        )
        await until(lambda: any(_is_audio(m) for m in received), what="an audio chunk")

        span = await body.dispatch.handle(
            command_for(welcome.payload.session, MICROPHONE_ID, "stop_capture", "spn_stop")
        )
        assert span is not None
        assert span.terminal == "succeeded"
        assert not body.capturing

        settled = len([m for m in received if _is_audio(m)])
        for _ in range(500):
            await asyncio.sleep(0)
        assert len([m for m in received if _is_audio(m)]) == settled
    finally:
        await body.client.close()


async def test_starting_twice_does_not_start_twice(
    brain: tuple[BrainServer, list[Any]],
) -> None:
    server, _received = brain
    body = stub_body(server, chunk_ms=20)
    welcome = await body.client.connect()

    try:
        await body.dispatch.handle(
            command_for(welcome.payload.session, MICROPHONE_ID, "start_capture", "spn_a")
        )
        first = body._capture_task

        span = await body.dispatch.handle(
            command_for(welcome.payload.session, MICROPHONE_ID, "start_capture", "spn_b")
        )
        assert span is not None
        assert span.terminal == "succeeded"
        assert span.span_id == "spn_b"
        assert body._capture_task is first
    finally:
        await body.stop_capture()
        await body.client.close()


async def test_a_latch_stops_capture(brain: tuple[BrainServer, list[Any]]) -> None:
    """A body told to stop should not still be listening."""
    server, _received = brain
    body = stub_body(server, chunk_ms=20)
    await body.client.connect()

    try:
        await body.dispatch.handle(
            command_for(body.client.session, MICROPHONE_ID, "start_capture", "spn_start")
        )
        assert body.capturing

        await body.estop("operator")

        assert not body.capturing
        assert body.state == "estopped"
    finally:
        await body.client.close()


async def test_captured_audio_round_trips_to_a_playable_wav(tmp_path: Path) -> None:
    """The step 4.2 done-check.

    Captured, chunked, sent, recorded, read back off disk, reassembled and
    written as a WAV that `wave` will open and report the declared format
    for. Playing it is then a matter of double-clicking the file.
    """
    path = tmp_path / "capture.mcap"
    recorder = FlightRecorder(path)
    recorder.open()

    audio_format = AudioFormat(sample_rate_hz=16000, channels=1)

    config = ServerConfig(host="127.0.0.1", port=0, auth_token=TOKEN)
    async with BrainServer(config, recorder=recorder) as server:
        body = stub_body(server, microphone=StubMicrophone(audio_format), chunk_ms=20)
        welcome = await body.client.connect()
        session = welcome.payload.session
        loops = asyncio.create_task(body.client.run_loops())

        state = server.sessions[session]
        start = command_for(session, MICROPHONE_ID, "start_capture", "spn_start")
        start.seq = state.record.outbound.take()
        server.registry.open_span(session, start)
        await state.send(start)

        await until(lambda: body.capturing, what="capture to start")
        await asyncio.sleep(0.1)
        await body.stop_capture()
        await asyncio.sleep(0.05)

        loops.cancel()
        await body.client.close()

    recorder.close()

    with path.open("rb") as handle:
        records = [
            (channel.topic, json.loads(message.data))
            for _schema, channel, message in make_reader(handle).iter_messages()
        ]

    chunks = [
        record
        for topic, record in records
        if topic == "/event" and record["message"]["payload"]["event"] == "audio_chunk"
    ]
    assert chunks, "no audio reached the recording"

    # Every chunk declares the same real format.
    for chunk in chunks:
        data = chunk["message"]["payload"]["data"]
        assert data["sample_rate_hz"] == 16000
        assert data["channels"] == 1
        assert data["encoding"] == PCM_S16LE

    pcm = b"".join(base64.b64decode(chunk["message"]["payload"]["data"]["b64"]) for chunk in chunks)
    reassembled = AudioChunk(pcm=pcm, format=audio_format)
    wav_path = tmp_path / "capture.wav"
    wav_path.write_bytes(reassembled.to_wav())

    with wave.open(str(wav_path), "rb") as handle:
        assert handle.getframerate() == 16000
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getnframes() == len(pcm) // 2
        assert handle.readframes(handle.getnframes()) == pcm

    assert set(pcm) != {0}, "the round trip produced silence"


def _is_audio(message: Any) -> bool:
    return message.type == "event" and message.payload.event == "audio_chunk"


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
