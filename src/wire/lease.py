"""Heartbeat leases, held by both sides of the protocol.

SPEC section 8.1 gives each side a lease on the other: the brain marks a body
LOST when it stops hearing, the body latches `safe_hold` when it stops
hearing the brain. Same mechanism, opposite directions, so it lives here
rather than in either one (ADR-0001: a body must not import brain internals).
"""

from __future__ import annotations

from wire.clock import SYSTEM_CLOCK, Clock


class LeaseWatch:
    """Has this body been quiet for longer than its lease?

    Measured on the monotonic clock, which cannot jump backwards when the
    wall clock is corrected. A wall-clock lease would expire an entire fleet
    at once on an NTP step.
    """

    __slots__ = ("_clock", "_last", "_lease_ns")

    def __init__(self, lease_ms: int, clock: Clock = SYSTEM_CLOCK, *, start: int | None = None):
        if lease_ms <= 0:
            raise ValueError(f"lease must be positive, got {lease_ms}ms")
        self._lease_ns = lease_ms * 1_000_000
        self._clock = clock
        self._last = clock.mono_ns() if start is None else start

    def beat(self) -> None:
        """A heartbeat arrived."""
        self._last = self._clock.mono_ns()

    @property
    def silent_ns(self) -> int:
        return self._clock.mono_ns() - self._last

    @property
    def silent_ms(self) -> float:
        return self.silent_ns / 1_000_000

    @property
    def expired(self) -> bool:
        return self.silent_ns >= self._lease_ns
