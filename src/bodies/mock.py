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
from bodies.commands import FAILED, CommandLedger, Outcome, rejected, succeeded
from bodies.dispatch import CommandDispatcher
from bodies.safety import LATCHED_SAFE_STATE, SafetyState
from wire import (
    ACTUATING_CLASSES,
    CommandEnvelope,
    EstopClearEnvelope,
    EstopEnvelope,
    Manifest,
    Message,
    Timestamp,
)
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

        manifest = mock_manifest(body_id)

        # Owned here, not by the client: a reconnect builds a new client and
        # must not clear a latch (SPEC section 8.2).
        self.safety = SafetyState(manifest.boot_state)

        self._actuating = {
            capability.id
            for capability in manifest.capabilities
            if capability.capability_class in ACTUATING_CLASSES
        }

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
        self._register_handlers()

    def _register_handlers(self) -> None:
        """One handler per action the manifest declares.

        The manifest is a promise. An action declared but unhandled would
        make it a lie, and the manifest is what the planner grounds against
        (ADR-0003), so the conformance suite checks this correspondence.
        """
        self.dispatch.on("sys", "ping", self._ping)
        self.dispatch.on("sys", "clear_safe_hold", self._clear_safe_hold)
        self.dispatch.on("drive0", "set_velocity", self._set_velocity)
        self.dispatch.on("drive0", "stop", self._stop)
        self.dispatch.on("range0", "read", self._read_range)

    async def _on_message(
        self, client: BodyClient, message: Message, received_at: Timestamp
    ) -> None:
        if isinstance(message, CommandEnvelope):
            await self.dispatch.handle(message, received_at)

    async def _on_priority(
        self, client: BodyClient, message: Message, received_at: Timestamp
    ) -> None:
        """E-stop and its release, handled ahead of queued work (section 8.3)."""
        if isinstance(message, EstopEnvelope):
            await self.estop(message.payload.reason)
        elif isinstance(message, EstopClearEnvelope):
            await self.clear_estop(message.payload.reason, message.payload.operator)

    # Safety transitions

    async def _announce(self, transition) -> None:
        """Put a state change on the wire, if it was a change.

        Best effort on purpose. The state has already changed locally by the
        time this runs, and the commonest reason to be latching at all is
        that the socket just died. A body that could not stop without a
        working network would be exactly backwards.
        """
        self.client.set_state(self.safety.state.value)
        if not transition.changed:
            return

        try:
            await self.client.emit_state(self.safety.state.value, cause=transition.cause)
        except Exception as exc:
            log.warning(
                "body %s: latched %s but could not announce it: %s",
                self.client.body_id,
                self.safety.state.value,
                exc,
            )

    async def estop(self, reason: str) -> None:
        """Cease actuation, latch, fail in-flight spans, announce (section 8.3).

        In that order. Stopping and latching are local and unconditional;
        announcing is the only part that needs a network, so it goes last.
        """
        self._pose.stop()
        transition = self.safety.estop(reason)
        await self._fail_actuation_spans(LATCHED_SAFE_STATE, f"estopped: {reason}")
        await self._announce(transition)

    async def clear_estop(self, reason: str, operator: str) -> None:
        """Release the E-stop into safe_hold, never straight into motion."""
        transition = self.safety.clear_estop(f"estop_clear by {operator}: {reason}")
        await self._announce(transition)

    async def enter_safe_hold(self, cause: str) -> None:
        """Latch safe_hold and stop, failing anything mid-actuation.

        Local work first, announcement last, for the same reason as `estop`.
        """
        self._pose.stop()
        transition = self.safety.enter_safe_hold(cause)
        if transition.changed:
            await self._fail_actuation_spans(LATCHED_SAFE_STATE, cause)
        await self._announce(transition)

    async def _fail_actuation_spans(self, code: str, message: str) -> None:
        """End every in-flight actuation span (SPEC section 8.1).

        Only actuation spans. A range read that happens to be in flight when
        the body latches is not dangerous and its result is still true.
        """
        for entry in self.ledger.outstanding():
            if entry.capability not in self._actuating:
                continue
            await self.dispatch.finish_span(
                entry, Outcome(status=FAILED, code=code, message=message)
            )

    async def watch_brain_lease(self) -> None:
        """Latch safe_hold if the brain goes quiet for lease_ms (section 8.1)."""
        lease = self.client.brain_lease
        if lease is None:
            return

        interval_s = (self.client.heartbeat_interval_ms or 1000) / 1000
        latched = False

        while True:
            await self._sleep(interval_s)

            if lease.expired:
                if not latched:
                    latched = True
                    log.warning(
                        "body %s: no brain heartbeat for %.0fms; latching safe_hold",
                        self.client.body_id,
                        lease.silent_ms,
                    )
                    # Nothing raised here may end this loop. A watchdog that
                    # dies on the first fault it sees is worse than none.
                    try:
                        await self.enter_safe_hold(
                            f"brain heartbeat lease missed after {lease.silent_ms:.0f}ms"
                        )
                    except Exception:
                        log.exception("body %s: latching raised", self.client.body_id)
            else:
                # The brain came back. The latch does not lift: only an
                # explicit clear_safe_hold does that (SPEC section 8.2).
                latched = False

    # Handlers

    def _ping(self, command: CommandEnvelope) -> Outcome:
        return succeeded(pong=True, body_id=self.client.body_id)

    async def _clear_safe_hold(self, command: CommandEnvelope) -> Outcome:
        """Release a latched safe_hold (SPEC section 8.2).

        The only way out of `safe_hold`. Refused while estopped, because
        each latch clears through its own path and clearing the milder one
        must not release the stronger.
        """
        transition = self.safety.clear_safe_hold()
        await self._announce(transition)

        if not transition.ok:
            return rejected(LATCHED_SAFE_STATE, transition.refused or "refused")

        return succeeded(state=self.safety.state.value, changed=transition.changed)

    async def _set_velocity(self, command: CommandEnvelope) -> Outcome:
        params = command.payload.params
        linear = float(params.get("linear_mps", 0.0))
        angular = float(params.get("angular_rps", 0.0))

        # The brain's validator grounds these against the manifest before
        # sending (ADR-0004). The body checks anyway: SPEC section 6.6 says
        # the body still enforces its own checks and MAY reject.
        # SPEC section 8.4: motion is permissioned, and a latched body is
        # not permitted, whatever the command says.
        if not self.safety.may_actuate:
            return rejected(
                LATCHED_SAFE_STATE,
                f"body is {self.safety.state.value}; actuation refused",
            )

        if abs(linear) > MAX_LINEAR_MPS or abs(angular) > MAX_ANGULAR_RPS:
            return rejected(
                "invalid_params",
                f"velocity outside declared bounds ({MAX_LINEAR_MPS} m/s, {MAX_ANGULAR_RPS} rad/s)",
            )

        self._pose.linear_mps = linear
        self._pose.angular_rps = angular
        return succeeded(linear_mps=linear, angular_rps=angular)

    def _stop(self, command: CommandEnvelope) -> Outcome:
        """Always allowed. Stopping is never the unsafe direction."""
        self._pose.stop()
        return succeeded(stopped=True)

    def _read_range(self, command: CommandEnvelope) -> Outcome:
        return succeeded(meters=self._fake_range(), valid=True)

    @property
    def pose(self) -> Pose:
        return self._pose

    @property
    def state(self) -> str:
        return self.safety.state.value

    async def run(self) -> None:
        """Connect, announce the state booted into, then report telemetry."""
        await self.client.connect()
        await self.client.announce_boot_state()

        background = [
            asyncio.create_task(self.telemetry_loop()),
            asyncio.create_task(self.watch_brain_lease()),
        ]
        try:
            await self.client.run_loops()
        finally:
            for task in background:
                task.cancel()
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
