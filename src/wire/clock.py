"""Time, as something you pass in rather than something you reach for.

Every lease, TTL, and heartbeat check in this codebase reads from a `Clock`.
Nothing in the logic calls `time.monotonic_ns()` directly, so safety-timing
tests advance a `ManualClock` and finish instantly instead of sleeping and
hoping. Timing tests that sleep are slow when they pass and flaky when they
fail, and a flaky safety test gets muted, which is the real cost.

Two clocks, deliberately, matching SPEC section 4:

`mono_ns` is monotonic and never jumps. Every deadline is measured on it, so
an NTP correction cannot expire a fleet of leases at once.

`utc` is the wall clock, comparable across devices and skewed by tens of
milliseconds. It is for correlation and for the log, never for a deadline.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

#: Where `ManualClock` starts when nothing says otherwise. A fixed instant,
#: so a test that prints a timestamp prints the same one tomorrow.
EPOCH_START = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


@runtime_checkable
class Clock(Protocol):
    """The only way this codebase learns what time it is."""

    def mono_ns(self) -> int:
        """Monotonic nanoseconds. For measuring durations and deadlines."""
        ...

    def utc(self) -> datetime:
        """Wall clock, timezone-aware UTC. For correlation and logs."""
        ...


class SystemClock:
    """Real time. The default everywhere outside tests."""

    __slots__ = ()

    def mono_ns(self) -> int:
        return time.monotonic_ns()

    def utc(self) -> datetime:
        return datetime.now(UTC)


class ManualClock:
    """Time under test control.

    Both clocks advance together by default, because that is what real time
    does. They can be moved apart on purpose, which is how the wall clock
    jumping (an NTP step, a laptop waking) gets tested without waiting for
    one to happen.
    """

    __slots__ = ("_mono_ns", "_utc")

    def __init__(self, *, mono_ns: int = 0, utc: datetime | None = None) -> None:
        self._mono_ns = mono_ns
        self._utc = utc if utc is not None else EPOCH_START

    def mono_ns(self) -> int:
        return self._mono_ns

    def utc(self) -> datetime:
        return self._utc

    def advance(self, *, seconds: float = 0, ms: float = 0, ns: int = 0) -> None:
        """Move both clocks forward by the same amount."""
        total_ns = int(seconds * 1_000_000_000) + int(ms * 1_000_000) + ns
        if total_ns < 0:
            raise ValueError("time does not run backwards; use jump_wall_clock to test skew")

        self._mono_ns += total_ns
        self._utc += timedelta(microseconds=total_ns / 1000)

    def jump_wall_clock(self, *, seconds: float = 0, ms: float = 0) -> None:
        """Move only the wall clock, forwards or backwards.

        What an NTP correction does. The monotonic clock is untouched, which
        is the whole reason deadlines are measured on it.
        """
        self._utc += timedelta(seconds=seconds, milliseconds=ms)


SYSTEM_CLOCK = SystemClock()
