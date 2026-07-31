"""The laptop body: the machine the brain is running on, as a body.

Step 4.1 gives it a camera. Microphone and speaker follow in 4.2 and 4.3.

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

from bodies.camera import (
    DEFAULT_HEIGHT,
    DEFAULT_WIDTH,
    CameraError,
    CaptureSource,
    OpenCVCamera,
)
from bodies.client import BodyClient, BodyConfig, Sleeper
from bodies.commands import CommandLedger, Outcome, failed, succeeded
from bodies.dispatch import CommandDispatcher
from bodies.safety import SafetyState
from wire import CommandEnvelope, Manifest, Message, Timestamp
from wire.clock import SYSTEM_CLOCK, Clock

log = logging.getLogger("bodies.laptop")

DEFAULT_BODY_ID = "laptop-01"
DEFAULT_URL = "ws://127.0.0.1:8765"

CAMERA_ID = "cam0"


def laptop_manifest(
    body_id: str = DEFAULT_BODY_ID,
    *,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    max_fps: int = 5,
) -> Manifest:
    """What this body declares (SPEC section 7.1).

    `actions` lists `snapshot` alone. The camera class registry also defines
    `start_stream` and `stop_stream`, and a subset is explicitly allowed
    (section 7.1). Declaring streaming before it exists would make the
    manifest a promise the body cannot keep, and the manifest is what the
    planner grounds against.
    """
    return Manifest.model_validate(
        {
            "body_id": body_id,
            "display_name": "Laptop body",
            "hardware_class": "workstation",
            # No actuation: nothing here can move (SPEC section 7.1).
            "boot_state": "ok",
            "adapter": {"name": "laptop-adapter", "version": "0.1.0"},
            "capabilities": [
                {
                    "id": "sys",
                    "class": "system",
                    "actions": ["ping", "clear_safe_hold"],
                    "events": ["state", "log"],
                },
                {
                    "id": CAMERA_ID,
                    "class": "camera",
                    "attributes": {
                        "formats": ["jpeg"],
                        "resolutions": [f"{width}x{height}"],
                        "max_fps": max_fps,
                    },
                    "actions": ["snapshot"],
                    "events": ["frame"],
                },
            ],
        }
    )


class LaptopBody:
    """Camera, on the machine you are sitting at."""

    def __init__(
        self,
        config: BodyConfig,
        *,
        body_id: str = DEFAULT_BODY_ID,
        camera: CaptureSource | None = None,
        clock: Clock = SYSTEM_CLOCK,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._camera = camera if camera is not None else OpenCVCamera()
        self._camera_open = False

        manifest = laptop_manifest(body_id)
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
        self.dispatch.on(CAMERA_ID, "snapshot", self._snapshot)

    @property
    def state(self) -> str:
        return self.safety.state.value

    @property
    def camera(self) -> CaptureSource:
        return self._camera

    async def run(self) -> None:
        await self.client.connect()
        await self.client.announce_boot_state()
        try:
            await self.client.run_loops()
        finally:
            self.close_camera()
            await self.client.close()

    def close_camera(self) -> None:
        if self._camera_open:
            with contextlib.suppress(Exception):
                self._camera.close()
            self._camera_open = False

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
        transition = self.safety.estop(reason)
        await self._announce(transition)

    async def clear_estop(self, reason: str, operator: str) -> None:
        await self._announce(self.safety.clear_estop(f"estop_clear by {operator}: {reason}"))

    async def enter_safe_hold(self, cause: str) -> None:
        self.close_camera()
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
        try:
            if not self._camera_open:
                self._camera.open()
                self._camera_open = True
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
