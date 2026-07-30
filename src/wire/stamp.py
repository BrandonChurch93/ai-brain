"""Stamping outbound messages: ids, timestamps, and per-sender sequence.

Both the brain and every body need these, so they live with the wire rather
than in either one.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime

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


def now() -> Timestamp:
    """t_captured for an outbound message (SPEC section 4).

    Two clocks on purpose. `mono_ns` orders messages within this process and
    cannot jump when the wall clock is corrected; `utc` correlates across
    devices, where tens of milliseconds of skew are expected and tolerated.
    """
    return Timestamp(
        mono_ns=time.monotonic_ns(),
        utc=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )


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
