"""The mock body: actuation semantics with zero hardware.

SPEC section 7.2 says `differential_drive` and `range_sensor` exist in v1
solely so this body can exercise TTL, safe_hold, and E-stop without anything
that can be damaged. It is also the reference implementation of the adapter
conformance checklist (SPEC section 10), so the test suite asserts against it
rather than against a real device that might be unplugged.

It boots into `safe_hold`, because it declares actuation and SPEC section 7.1
requires that. Motion is permissioned: stopped is the default and stays that
way until something explicitly clears it (SPEC section 8.4).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
from dataclasses import dataclass

from bodies.client import BodyClient, BodyConfig, Sleeper
from wire import Manifest
from wire.clock import SYSTEM_CLOCK, Clock

log = logging.getLogger("bodies.mock")

DEFAULT_BODY_ID = "mock-01"
DEFAULT_URL = "ws://127.0.0.1:8765"

#: How often the fake sensors report. Modest by design: v1 carries media and
#: telemetry as JSON, and sustained high rates are the reserved binary seam,
#: not a v1 feature (SPEC section 6.5).
DEFAULT_TELEMETRY_MS = 500

#: Bounds the brain's validator grounds `set_velocity` against (ADR-0004).
MAX_LINEAR_MPS = 0.4
MAX_ANGULAR_RPS = 1.2

RANGE_MIN_M = 0.05
RANGE_MAX_M = 4.0


def mock_manifest(body_id: str = DEFAULT_BODY_ID) -> Manifest:
    """What this body declares it can do (SPEC section 7.1).

    Every action and event here is drawn from the class registry in SPEC
    section 7.2. Declaring anything outside it would be a manifest the
    validator could not ground commands against.
    """
    return Manifest.model_validate(
        {
            "body_id": body_id,
            "display_name": "Mock body",
            "hardware_class": "virtual",
            # Declares actuation, so it MUST boot stopped (SPEC section 7.1).
            "boot_state": "safe_hold",
            "adapter": {"name": "mock-adapter", "version": "0.1.0"},
            "capabilities": [
                {
                    "id": "sys",
                    "class": "system",
                    "actions": ["ping", "clear_safe_hold"],
                    "events": ["state", "log"],
                },
                {
                    "id": "drive0",
                    "class": "differential_drive",
                    "attributes": {
                        "max_linear_mps": MAX_LINEAR_MPS,
                        "max_angular_rps": MAX_ANGULAR_RPS,
                    },
                    "actions": ["set_velocity", "stop"],
                    "events": ["odometry"],
                },
                {
                    "id": "range0",
                    "class": "range_sensor",
                    "attributes": {
                        "min_m": RANGE_MIN_M,
                        "max_m": RANGE_MAX_M,
                        "fov_deg": 25,
                    },
                    "actions": ["read"],
                    "events": ["range"],
                },
            ],
        }
    )


@dataclass(slots=True)
class Pose:
    """Dead-reckoned position. Fake, but integrated rather than random, so a
    reader can tell a body that moved from one that did not."""

    x_m: float = 0.0
    y_m: float = 0.0
    heading_rad: float = 0.0
    linear_mps: float = 0.0
    angular_rps: float = 0.0

    def integrate(self, elapsed_s: float) -> None:
        self.heading_rad = (self.heading_rad + self.angular_rps * elapsed_s) % (2 * math.pi)
        self.x_m += self.linear_mps * elapsed_s * math.cos(self.heading_rad)
        self.y_m += self.linear_mps * elapsed_s * math.sin(self.heading_rad)

    def stop(self) -> None:
        self.linear_mps = 0.0
        self.angular_rps = 0.0


class MockBody:
    """A body with a drive it cannot crash and a rangefinder that sees nothing."""

    def __init__(
        self,
        config: BodyConfig,
        *,
        body_id: str = DEFAULT_BODY_ID,
        telemetry_ms: int = DEFAULT_TELEMETRY_MS,
        clock: Clock = SYSTEM_CLOCK,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        self._telemetry_ms = telemetry_ms
        self._clock = clock
        self._sleep = sleep
        self._pose = Pose()
        self._ticks = 0

        self.client = BodyClient(
            mock_manifest(body_id),
            config,
            clock=clock,
            sleep=sleep,
        )

    @property
    def pose(self) -> Pose:
        return self._pose

    @property
    def state(self) -> str:
        return self.client.state

    async def run(self) -> None:
        """Connect, announce the state booted into, then report telemetry."""
        await self.client.connect()
        await self.client.announce_boot_state()

        telemetry = asyncio.create_task(self.telemetry_loop())
        try:
            await self.client.run_loops()
        finally:
            telemetry.cancel()
            await self.client.close()

    async def telemetry_loop(self) -> None:
        """Odometry and range on a timer (SPEC section 7.2 standard events)."""
        interval_s = self._telemetry_ms / 1000

        while True:
            await self._sleep(interval_s)
            try:
                await self.emit_telemetry(interval_s)
            except Exception:  # a dead socket ends the session, not the process
                log.debug("body %s: telemetry send failed", self.client.body_id)
                return

    async def emit_telemetry(self, elapsed_s: float) -> None:
        """One round of fake sensor readings."""
        self._ticks += 1
        self._pose.integrate(elapsed_s)

        await self.client.emit(
            "drive0",
            "odometry",
            {
                "x_m": round(self._pose.x_m, 4),
                "y_m": round(self._pose.y_m, 4),
                "heading_rad": round(self._pose.heading_rad, 4),
                "linear_mps": self._pose.linear_mps,
                "angular_rps": self._pose.angular_rps,
            },
            # Telemetry the brain can afford to miss, unlike a state change.
            droppable=True,
        )

        await self.client.emit(
            "range0",
            "range",
            {"meters": self._fake_range(), "valid": True},
            droppable=True,
        )

    def _fake_range(self) -> float:
        """A wall the body slowly approaches and then backs away from.

        Deterministic on purpose: a replayed session must produce the same
        readings, and a random sensor would make ADR-0005's identical-replay
        promise untestable.
        """
        span = RANGE_MAX_M - RANGE_MIN_M
        phase = (self._ticks % 20) / 20
        return round(RANGE_MIN_M + span * abs(1 - 2 * phase), 3)


def config_from_env(env: dict[str, str] | None = None) -> BodyConfig:
    """Body-side config. The brain URL belongs here (SPEC section 3.1)."""
    source = os.environ if env is None else env
    return BodyConfig(
        url=source.get("BRAIN_URL", DEFAULT_URL),
        auth_token=source.get("BRAIN_AUTH_TOKEN", ""),
    )


async def serve(env: dict[str, str] | None = None) -> None:
    source = os.environ if env is None else env
    body = MockBody(
        config_from_env(source),
        body_id=source.get("BRAIN_BODY_ID", DEFAULT_BODY_ID),
    )
    await body.run()


def main() -> None:
    """Entry point for running the mock body as its own process."""
    logging.basicConfig(
        level=os.environ.get("BRAIN_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    # Ctrl-C is how an operator stops a body; it is not an error.
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve())


if __name__ == "__main__":
    main()
