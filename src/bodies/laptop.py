"""The laptop body: the machine the brain is running on, as a body.

Steps 4.1 through 4.3 give it a camera, a microphone, and a speaker.

The manifest is built from what the devices actually opened at, never from
what was asked for. Hardware substitutes silently: a camera can hand back a
different resolution and a microphone a different sample rate, and step 6.1
feeds this audio to Whisper, which cares about the real rate.

It boots into `ok`, not `safe_hold`. SPEC section 7.1 requires `safe_hold`
of a body that can move, and this one cannot: the worst a camera does is
take a picture. Sensor-only bodies MAY boot `ok`, and pretending otherwise
would mean every session began by clearing a hold that protected nothing.

The capture source is injected. That is what lets this exact adapter run in
CI against a stub, so the conformance suite holds it to the same SPEC
section 10 checklist as the mock without needing a camera or a human to
approve one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from bodies.audio import (
    DEFAULT_CHUNK_MS,
    AudioSource,
    MicrophoneError,
    SoundDeviceMicrophone,
)
from bodies.camera import (
    CameraError,
    CaptureSource,
    OpenCVCamera,
)
from bodies.client import BodyClient, BodyConfig, Sleeper
from bodies.commands import (
    RUNNING,
    CommandLedger,
    Outcome,
    failed,
    interrupted,
    succeeded,
)
from bodies.dispatch import CommandDispatcher
from bodies.safety import SafetyState
from bodies.speech import MacSaySpeaker, Speaker, SpeechError
from wire import CommandEnvelope, Manifest, Message, Timestamp
from wire.clock import SYSTEM_CLOCK, Clock

log = logging.getLogger("bodies.laptop")

DEFAULT_BODY_ID = "laptop-01"
DEFAULT_URL = "ws://127.0.0.1:8765"

CAMERA_ID = "cam0"
MICROPHONE_ID = "mic0"
SPEAKER_ID = "spk0"

DEFAULT_MAX_FPS = 5


def laptop_manifest(
    body_id: str = DEFAULT_BODY_ID,
    *,
    camera: dict[str, object] | None = None,
    microphone: dict[str, object] | None = None,
    speaker: dict[str, object] | None = None,
) -> Manifest:
    """What this body declares (SPEC section 7.1).

    Attributes are passed in, not assumed, because they come from what the
    devices actually opened at. A camera asked for 1280x720 may hand back
    640x480 and a microphone asked for 16 kHz may hand back 48 kHz, and both
    do so silently. Declaring the request rather than the reality would make
    the manifest a lie the planner grounds against, and would send Whisper
    audio at a rate it was not told about (step 6.1).

    A device that did not open is left out entirely. A manifest naming a
    capability the body cannot deliver is worse than a smaller manifest.

    `actions` lists `snapshot` alone for the camera. The class registry also
    defines `start_stream` and `stop_stream`; a subset is explicitly allowed
    (section 7.1), and declaring streaming before it exists would be another
    promise the body cannot keep.
    """
    capabilities: list[dict[str, object]] = [
        {
            "id": "sys",
            "class": "system",
            "actions": ["ping", "clear_safe_hold"],
            "events": ["state", "log"],
        }
    ]

    if camera is not None:
        capabilities.append(
            {
                "id": CAMERA_ID,
                "class": "camera",
                "attributes": camera,
                "actions": ["snapshot"],
                "events": ["frame"],
            }
        )

    if microphone is not None:
        capabilities.append(
            {
                "id": MICROPHONE_ID,
                "class": "microphone",
                "attributes": microphone,
                "actions": ["start_capture", "stop_capture"],
                "events": ["audio_chunk"],
            }
        )

    if speaker is not None:
        capabilities.append(
            {
                "id": SPEAKER_ID,
                "class": "speaker",
                "attributes": speaker,
                # `play` takes an audio payload and is not implemented, so it
                # is not declared. A subset is allowed (section 7.1).
                "actions": ["say", "stop"],
                "events": ["playback_state"],
            }
        )

    return Manifest.model_validate(
        {
            "body_id": body_id,
            "display_name": "Laptop body",
            "hardware_class": "workstation",
            # Nothing here moves, so it may boot ok (SPEC section 7.1).
            "boot_state": "ok",
            "adapter": {"name": "laptop-adapter", "version": "0.1.0"},
            "capabilities": capabilities,
        }
    )


class LaptopBody:
    """Camera and microphone, on the machine you are sitting at."""

    def __init__(
        self,
        config: BodyConfig,
        *,
        body_id: str = DEFAULT_BODY_ID,
        camera: CaptureSource | None = None,
        microphone: AudioSource | None = None,
        speaker: Speaker | None = None,
        chunk_ms: int = DEFAULT_CHUNK_MS,
        clock: Clock = SYSTEM_CLOCK,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._chunk_ms = chunk_ms

        self._camera = camera if camera is not None else OpenCVCamera()
        self._microphone = microphone if microphone is not None else SoundDeviceMicrophone()
        self._speaker = speaker if speaker is not None else MacSaySpeaker()
        self._camera_open = False
        self._audio_format = None
        self._speech_format = None
        self._capture_task: asyncio.Task | None = None
        self._capture_trace: str | None = None
        self._speech_task: asyncio.Task | None = None
        self._speaking_span = None

        manifest = laptop_manifest(body_id, **self._probe())
        self.safety = SafetyState(manifest.boot_state)

        self.client = BodyClient(
            manifest,
            config,
            on_message=self._on_message,
            on_priority=self._on_priority,
            state=self.safety.state.value,
            clock=clock,
            sleep=sleep,
        )
        self.ledger = CommandLedger(clock)
        self.dispatch = CommandDispatcher(self.client, self.ledger)

        self.dispatch.on("sys", "ping", self._ping)
        self.dispatch.on("sys", "clear_safe_hold", self._clear_safe_hold)
        if self.has_camera:
            self.dispatch.on(CAMERA_ID, "snapshot", self._snapshot)
        if self.has_microphone:
            self.dispatch.on(MICROPHONE_ID, "start_capture", self._start_capture)
            self.dispatch.on(MICROPHONE_ID, "stop_capture", self._stop_capture)
        if self.has_speaker:
            self.dispatch.on(SPEAKER_ID, "say", self._say)
            self.dispatch.on(SPEAKER_ID, "stop", self._stop_saying)

    def _probe(self) -> dict[str, dict[str, object] | None]:
        """Open each device to learn what it really is.

        Done before the handshake so the manifest describes reality. A device
        that will not open is reported and left out: the alternative is a
        manifest that promises what the body cannot do, and the planner
        grounds its plans on that promise (ADR-0003).

        This is also where a macOS permission prompt appears, which is the
        right moment for it: at startup, attached to launching the body,
        rather than in the middle of a mission.

        Once, at startup, for v1. A body does not re-probe mid-session, so a
        device plugged in after launch is not noticed until a restart. Making
        that work is additive rather than breaking: a body could re-probe and
        answer `list_capabilities` with a fresh manifest, and v1 simply does
        not define what mid-session capability appearance means. It would
        need one, since a planner holding the old manifest is entitled to
        assume it is still true.
        """
        attributes: dict[str, dict[str, object] | None] = {"camera": None, "microphone": None}

        try:
            frame_format = self._camera.open()
            self._camera_open = True
            attributes["camera"] = frame_format.as_attributes(DEFAULT_MAX_FPS)
        except CameraError as exc:
            log.warning("no camera capability: %s", exc)

        try:
            self._audio_format = self._microphone.open()
            attributes["microphone"] = self._audio_format.as_attributes()
        except MicrophoneError as exc:
            log.warning("no microphone capability: %s", exc)

        try:
            self._speech_format = self._speaker.open()
            attributes["speaker"] = self._speech_format.as_attributes()
        except SpeechError as exc:
            log.warning("no speaker capability: %s", exc)

        return attributes

    @property
    def state(self) -> str:
        return self.safety.state.value

    @property
    def camera(self) -> CaptureSource:
        return self._camera

    @property
    def microphone(self) -> AudioSource:
        return self._microphone

    @property
    def speaker(self) -> Speaker:
        return self._speaker

    @property
    def has_camera(self) -> bool:
        return self._camera_open

    @property
    def has_microphone(self) -> bool:
        return self._audio_format is not None

    @property
    def has_speaker(self) -> bool:
        return self._speech_format is not None

    @property
    def speaking(self) -> bool:
        return self._speech_task is not None and not self._speech_task.done()

    @property
    def capturing(self) -> bool:
        return self._capture_task is not None and not self._capture_task.done()

    async def run(self) -> None:
        await self.client.connect()
        await self.client.announce_boot_state()
        try:
            await self.client.run_loops()
        finally:
            await self.stop_capture()
            await self._interrupt_speech("shutting down")
            self.close_camera()
            self.close_microphone()
            self._speaker.close()
            await self.client.close()

    def close_camera(self) -> None:
        if self._camera_open:
            with contextlib.suppress(Exception):
                self._camera.close()
            self._camera_open = False

    def close_microphone(self) -> None:
        if self._audio_format is not None:
            with contextlib.suppress(Exception):
                self._microphone.close()
            self._audio_format = None

    async def emit_telemetry(self, elapsed_s: float) -> None:
        """No periodic telemetry. A camera reports when asked, not on a timer.

        Streaming is what a timer would be for, and this body does not declare
        it (see `laptop_manifest`).
        """
        return

    # Safety, shared shape with every other adapter

    async def _on_message(
        self, client: BodyClient, message: Message, received_at: Timestamp
    ) -> None:
        if isinstance(message, CommandEnvelope):
            await self.dispatch.handle(message, received_at)

    async def _on_priority(
        self, client: BodyClient, message: Message, received_at: Timestamp
    ) -> None:
        from wire import EstopClearEnvelope, EstopEnvelope

        if isinstance(message, EstopEnvelope):
            await self.estop(message.payload.reason)
        elif isinstance(message, EstopClearEnvelope):
            await self.clear_estop(message.payload.reason, message.payload.operator)

    async def _announce(self, transition) -> None:
        self.client.set_state(self.safety.state.value)
        if not transition.changed:
            return
        try:
            await self.client.emit_state(self.safety.state.value, cause=transition.cause)
        except Exception as exc:
            log.warning("body %s: latched but could not announce: %s", self.client.body_id, exc)

    async def estop(self, reason: str) -> None:
        """Stop taking pictures and latch (SPEC section 8.3).

        A camera cannot hurt anyone, but E-stop is a global state and a body
        that ignored it would report `ok` while the rest of the system was
        stopped, which is worse than useless in a log.
        """
        self.close_camera()
        await self.stop_capture()
        await self._interrupt_speech("estopped")
        transition = self.safety.estop(reason)
        await self._announce(transition)

    async def clear_estop(self, reason: str, operator: str) -> None:
        await self._announce(self.safety.clear_estop(f"estop_clear by {operator}: {reason}"))

    async def enter_safe_hold(self, cause: str) -> None:
        self.close_camera()
        await self.stop_capture()
        await self._interrupt_speech("safe hold")
        await self._announce(self.safety.enter_safe_hold(cause))

    # Handlers

    def _ping(self, command: CommandEnvelope) -> Outcome:
        return succeeded(pong=True, body_id=self.client.body_id)

    async def _clear_safe_hold(self, command: CommandEnvelope) -> Outcome:
        transition = self.safety.clear_safe_hold()
        await self._announce(transition)
        if not transition.ok:
            return Outcome(
                status="rejected",
                code="latched_safe_state",
                message=transition.refused or "refused",
            )
        return succeeded(state=self.safety.state.value, changed=transition.changed)

    async def _start_capture(self, command: CommandEnvelope) -> Outcome:
        """Begin push-to-talk capture (SPEC section 7.2).

        Returns as soon as capture is running rather than when it ends. The
        span is the act of starting, not the recording: a command whose
        result waited for the operator to let go of the button would sit past
        its own TTL every time (SPEC section 6.6).
        """
        if self._audio_format is None:
            return failed("microphone_unavailable", "this body has no microphone")

        if self.capturing:
            return succeeded(capturing=True, changed=False)

        self._capture_trace = command.trace_id
        self._capture_task = asyncio.create_task(self._capture_loop())

        return succeeded(
            capturing=True,
            changed=True,
            **self._audio_format.as_attributes(),
        )

    async def _stop_capture(self, command: CommandEnvelope) -> Outcome:
        chunks = await self.stop_capture()
        return succeeded(capturing=False, chunks=chunks)

    async def stop_capture(self) -> int:
        """End capture and report how many chunks were sent."""
        task = self._capture_task
        self._capture_task = None
        self._capture_trace = None

        if task is None:
            return 0

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        return getattr(task, "_chunks_sent", 0)

    async def _capture_loop(self) -> None:
        """Read the microphone and emit a chunk at a time.

        Chunked rather than one blob at the end so a long press does not sit
        silent and then arrive all at once, and so a session killed mid-press
        still holds whatever was said before it died.
        """
        seconds = self._chunk_ms / 1000
        sent = 0
        task = asyncio.current_task()

        try:
            while True:
                chunk = await asyncio.to_thread(self._microphone.read, seconds)
                await self.client.emit(
                    MICROPHONE_ID,
                    "audio_chunk",
                    chunk.as_event_data(),
                    # Speech is not telemetry. A dropped chunk is a hole in a
                    # sentence, and the next one does not replace it.
                    droppable=False,
                    trace_id=self._capture_trace,
                )
                sent += 1
                if task is not None:
                    task._chunks_sent = sent  # type: ignore[attr-defined]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("body %s: capture stopped: %s", self.client.body_id, exc)

    async def _say(self, command: CommandEnvelope) -> Outcome | None:
        """Speak a sentence (SPEC section 7.2).

        The span is deferred, not answered. A TTL governs beginning rather
        than duration (SPEC section 6.6), so this reports `running` the
        moment playback starts and ends `succeeded` when the sentence
        finishes. A say that started is never expired, however long it runs.

        Anything already being said is interrupted first. Two voices at once
        is never what was wanted, and the interrupted span ends properly
        rather than being abandoned.
        """
        text = str(command.payload.params.get("text", "")).strip()
        if not text:
            return failed("invalid_params", "say needs a non-empty `text`")

        entry = self.ledger.get(command.span_id)
        if entry is None:  # pragma: no cover - the dispatcher just admitted it
            return failed("internal", "span vanished")

        await self._interrupt_speech("superseded by a later say")

        try:
            await self._speaker.start(text)
        except SpeechError as exc:
            return failed("speaker_unavailable", str(exc))

        # Told before the audio matters: a voice loop gates the microphone on
        # this, and being told after the first syllable is too late (6.3).
        await self._emit_playback("started", command.trace_id, text=text)
        await self.dispatch.report(entry, RUNNING, progress=0.0, text=text)

        self._speaking_span = entry
        self._speech_task = asyncio.create_task(self._finish_saying(entry, command.trace_id))
        return None

    async def _finish_saying(self, entry, trace_id: str | None) -> None:
        """Wait out the sentence, then close its span however it ended."""
        try:
            completed = await self._speaker.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("body %s: playback failed: %s", self.client.body_id, exc)
            await self._emit_playback("stopped", trace_id, reason="failed")
            await self.dispatch.finish_span(entry, failed("speaker_unavailable", str(exc)))
            return
        finally:
            self._speaking_span = None

        reason = "completed" if completed else "interrupted"
        await self._emit_playback("stopped", trace_id, reason=reason)

        outcome = (
            succeeded(text_spoken=True)
            if completed
            else interrupted("playback was stopped before the sentence finished")
        )
        await self.dispatch.finish_span(entry, outcome)

    async def _stop_saying(self, command: CommandEnvelope) -> Outcome:
        """Cut off whatever is being said. The barge-in primitive for 6.3."""
        stopped = await self._interrupt_speech("stopped by command")
        return succeeded(stopped=stopped)

    async def _interrupt_speech(self, reason: str) -> bool:
        """Stop playback and let the interrupted span end properly.

        Returns whether anything was actually interrupted.
        """
        if self._speech_task is None:
            return False

        task, self._speech_task = self._speech_task, None
        if task.done():
            return False

        await self._speaker.stop()
        # `wait` returns False once stopped, so the waiting task closes the
        # span as interrupted on its own. Awaiting it here is what makes the
        # interruption observable by the time `stop` answers.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        return True

    async def _emit_playback(self, state: str, trace_id: str | None, **data: object) -> None:
        """Announce playback starting or stopping (SPEC section 7.2).

        Carries the say's `trace_id`, so a voice loop can tell its own speech
        from someone else's and gate the microphone against hearing the body
        talk to itself (step 6.3).
        """
        try:
            await self.client.emit(
                SPEAKER_ID,
                "playback_state",
                {"state": state, **data},
                # Not droppable. A missed `stopped` leaves a voice loop with
                # the microphone gated forever, deaf and unaware.
                droppable=False,
                trace_id=trace_id,
            )
        except Exception as exc:
            log.warning("body %s: could not announce playback: %s", self.client.body_id, exc)

    async def _snapshot(self, command: CommandEnvelope) -> Outcome:
        """Take one picture and put it on the perception stream.

        The image goes out as a `frame` event carrying the command's
        `trace_id`, and the result reports the shape rather than repeating the
        payload. Frames belong on the frame channel, where a consumer looking
        for perception will find them, and sending the same base64 twice would
        double the cost of the largest thing v1 puts on the wire.

        The event is not droppable. SPEC section 6.5 marks streamed frames
        droppable because a dropped one is followed by another; a snapshot was
        asked for, and dropping it would answer a question with silence.
        """
        if not self._camera_open:
            return failed("camera_unavailable", "this body has no camera")

        try:
            frame = self._camera.capture()
        except CameraError as exc:
            self.close_camera()
            log.warning("body %s: snapshot failed: %s", self.client.body_id, exc)
            return failed("camera_unavailable", str(exc))

        await self.client.emit(
            CAMERA_ID,
            "frame",
            frame.as_event_data(),
            droppable=False,
            trace_id=command.trace_id,
        )

        return succeeded(
            format="jpeg",
            width=frame.width,
            height=frame.height,
            bytes=len(frame.jpeg),
        )


def config_from_env(env: dict[str, str] | None = None) -> BodyConfig:
    source = os.environ if env is None else env
    return BodyConfig(
        url=source.get("BRAIN_URL", DEFAULT_URL),
        auth_token=source.get("BRAIN_AUTH_TOKEN", ""),
    )


async def serve(env: dict[str, str] | None = None) -> None:
    source = os.environ if env is None else env
    body = LaptopBody(
        config_from_env(source),
        body_id=source.get("BRAIN_BODY_ID", DEFAULT_BODY_ID),
    )
    await body.run()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("BRAIN_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve())


if __name__ == "__main__":
    main()
