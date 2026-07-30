"""Brain-side heartbeats and lease detection (SPEC sections 6.4 and 8.1).

Both sides beat on the interval the brain named in `welcome`. Silence for
`lease_ms` is a lease miss. The two directions are independent and neither
waits on the other: the body latches `safe_hold` off the brain's silence,
the brain marks the body LOST off the body's silence. Either half works with
the other half dead, which is the point (ADR-0006).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

from wire import HeartbeatEnvelope, HeartbeatPayload
from wire.stamp import SeqCounter, new_id, now

log = logging.getLogger("brain.heartbeat")

#: What the brain reports about itself. `active` normally, `degraded` when
#: the LLM or network is unavailable. Bodies may treat it as advisory; the
#: wire contract does not change (SPEC section 8.5). The full brain FSM is
#: checklist step 5.4.
BrainState = str

Clock = Callable[[], int]


class LeaseWatch:
    """Has this body been quiet for longer than its lease?

    The clock is injectable so lease logic can be tested without waiting.
    It reads monotonic nanoseconds, which cannot jump backwards when the
    wall clock is corrected. A wall-clock lease could expire an entire fleet
    at once on an NTP step.
    """

    __slots__ = ("_clock", "_last", "_lease_ns")

    def __init__(self, lease_ms: int, clock: Clock, *, start: int | None = None) -> None:
        if lease_ms <= 0:
            raise ValueError(f"lease must be positive, got {lease_ms}ms")
        self._lease_ns = lease_ms * 1_000_000
        self._clock = clock
        self._last = clock() if start is None else start

    def beat(self) -> None:
        """A heartbeat arrived."""
        self._last = self._clock()

    @property
    def silent_ns(self) -> int:
        return self._clock() - self._last

    @property
    def silent_ms(self) -> float:
        return self.silent_ns / 1_000_000

    @property
    def expired(self) -> bool:
        return self.silent_ns >= self._lease_ns


def brain_heartbeat(state: BrainState, session: str, seq: SeqCounter) -> HeartbeatEnvelope:
    """One outbound heartbeat (SPEC section 6.4)."""
    return HeartbeatEnvelope(
        type="heartbeat",
        id=new_id(),
        session=session,
        seq=seq.take(),
        ts=now(),
        payload=HeartbeatPayload(state=state),  # type: ignore[arg-type]
    )


async def heartbeat_loop(
    *,
    session: str,
    interval_ms: int,
    watch: LeaseWatch,
    seq: SeqCounter,
    send: Callable[[HeartbeatEnvelope], object],
    brain_state: Callable[[], BrainState],
    on_lost: Callable[[float], object],
    on_recovered: Callable[[], object] | None = None,
) -> None:
    """Beat on the interval; report a lease miss when the body goes quiet.

    Sending and lease checking share one loop deliberately. If the send
    blocked on a backpressured socket, a separate checker would keep
    declaring the body healthy while nothing was actually getting through.

    A lease miss does not end the loop. The socket may still be open, the
    body may come back, and the brain wants to notice that too.
    """
    interval_s = interval_ms / 1000
    reported = False

    while True:
        await asyncio.sleep(interval_s)

        with contextlib.suppress(Exception):
            result = send(brain_heartbeat(brain_state(), session, seq))
            if asyncio.iscoroutine(result):
                await result

        if watch.expired:
            if not reported:
                reported = True
                silent = watch.silent_ms
                log.warning(
                    "session %s: no body heartbeat for %.0fms, lease missed",
                    session,
                    silent,
                )
                outcome = on_lost(silent)
                if asyncio.iscoroutine(outcome):
                    await outcome
        elif reported:
            reported = False
            if on_recovered is not None:
                outcome = on_recovered()
                if asyncio.iscoroutine(outcome):
                    await outcome
