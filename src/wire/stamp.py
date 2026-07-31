"""Stamping outbound messages: ids, timestamps, and per-sender sequence.

Both the brain and every body need these, so they live with the wire rather
than in either one.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from wire.clock import SYSTEM_CLOCK, Clock
from wire.models import Timestamp


def new_id() -> str:
    """A unique message id.

    SPEC section 4 recommends ULID or UUIDv7 for their sortability. v1 uses
    uuid4 to avoid a dependency: ordering already comes from `seq` per sender
    and `ts.mono_ns` within a device, so nothing here relies on ids sorting.
    """
    return uuid.uuid4().hex


def new_session_id() -> str:
    return f"sess_{uuid.uuid4().hex}"


def now(clock: Clock = SYSTEM_CLOCK) -> Timestamp:
    """t_captured for an outbound message (SPEC section 4).

    Two clocks on purpose. `mono_ns` orders messages within this process and
    cannot jump when the wall clock is corrected; `utc` correlates across
    devices, where tens of milliseconds of skew are expected and tolerated.

    The clock is a parameter so timing behaviour can be driven by tests. The
    default is real time, so callers that do not care need not say so.
    """
    return Timestamp(
        mono_ns=clock.mono_ns(),
        utc=clock.utc().isoformat().replace("+00:00", "Z"),
    )


def epoch_ns(stamp: Timestamp) -> int:
    """`stamp.utc` as nanoseconds since the epoch.

    The flight recorder needs one timeline that means the same thing on
    every device. `mono_ns` cannot be it: monotonic clocks count from an
    arbitrary per-machine origin, so a body's and the brain's are not
    comparable. The wall clock is comparable, skewed by tens of
    milliseconds, and that is the trade MCAP timestamps want (ADR-0005).
    `mono_ns` is kept alongside for exact ordering within one device.
    """
    return int(datetime.fromisoformat(stamp.utc).timestamp() * 1_000_000_000)


class SeqCounter:
    """Per-sender counter: starts at 1, increments by 1 per message sent,
    resets per connection (SPEC section 4)."""

    __slots__ = ("_next",)

    def __init__(self) -> None:
        self._next = 1

    def take(self) -> int:
        value = self._next
        self._next += 1
        return value

    @property
    def peek(self) -> int:
        return self._next
