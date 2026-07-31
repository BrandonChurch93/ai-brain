"""The session registry: who is connected, and what has arrived from them.

SPEC section 4 makes two ordering promises, and this holds both ends of
them. Within one sender and one session, `seq` orders messages. Across
devices, brain-side `t_received` orders them, because sender clocks drift by
tens of milliseconds and that is expected rather than a fault.

Deliberately free of sockets, so the bookkeeping can be tested without a
network and reused by anything else that receives protocol messages.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from wire import CommandEnvelope, CommandResultEnvelope, Manifest, Message, Timestamp
from wire.clock import SYSTEM_CLOCK, Clock
from wire.stamp import SeqCounter, now

log = logging.getLogger("brain.registry")


class SeqStatus(Enum):
    """What the sequence number said about this message.

    The transport is TCP, so none of these mean "the network dropped
    something". They mean the sender's own counter did not do what SPEC
    section 4 requires, which is a bug in that body worth seeing.
    """

    IN_ORDER = "in_order"
    GAP = "gap"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class Reception:
    """One inbound message, as the brain saw it."""

    message: Message
    t_received: Timestamp
    seq_status: SeqStatus
    #: How many sequence numbers were skipped. Zero unless `seq_status` is GAP.
    missing: int = 0
    #: The message named a session other than the one it arrived on.
    session_mismatch: bool = False

    @property
    def is_suspect(self) -> bool:
        return self.seq_status is not SeqStatus.IN_ORDER or self.session_mismatch


class SeqTracker:
    """Per-sender inbound counter (SPEC section 4).

    Seeded from `hello`, which is the sender's first message and therefore
    establishes where its counter started.
    """

    __slots__ = ("_last",)

    def __init__(self, last: int = 0) -> None:
        self._last = last

    @property
    def last(self) -> int:
        return self._last

    def observe(self, seq: int) -> tuple[SeqStatus, int]:
        expected = self._last + 1

        if seq == expected:
            self._last = seq
            return SeqStatus.IN_ORDER, 0

        if seq > expected:
            missing = seq - expected
            # Advance anyway: refusing to move on would report every later
            # message as a gap too, turning one fault into an endless stream.
            self._last = seq
            return SeqStatus.GAP, missing

        return SeqStatus.DUPLICATE, 0


#: Failure code the brain writes on spans it gives up on when a body goes
#: quiet. Distinct from the body's own `latched_safe_state`, because these
#: two describe different things: the body knows it stopped, the brain only
#: knows it stopped hearing.
BODY_LOST = "body_lost"


@dataclass(frozen=True, slots=True)
class SpanRecord:
    """One command the brain sent and is still waiting on (SPEC section 6.6)."""

    span_id: str
    trace_id: str | None
    capability: str
    action: str
    ttl_ms: int
    sent_at: Timestamp


@dataclass(frozen=True, slots=True)
class SpanOutcome:
    """How a span ended. Terminal, and there is exactly one per span."""

    span: SpanRecord
    status: str
    code: str | None = None
    message: str | None = None


@dataclass(slots=True)
class SessionRecord:
    """One accepted connection, from `welcome` to socket close."""

    session: str
    body_id: str
    protocol_version: str
    manifest: Manifest
    opened_at: Timestamp
    inbound: SeqTracker = field(default_factory=SeqTracker)
    outbound: SeqCounter = field(default_factory=SeqCounter)
    #: Commands sent and not yet terminal, keyed by span id.
    spans: dict[str, SpanRecord] = field(default_factory=dict)
    #: The brain has stopped hearing this body's heartbeats (SPEC section 8.1).
    lost: bool = False


class SessionRegistry:
    """Every open session, keyed by session id."""

    def __init__(self, clock: Clock = SYSTEM_CLOCK) -> None:
        self._records: dict[str, SessionRecord] = {}
        self._clock = clock

    def open(
        self,
        *,
        session: str,
        body_id: str,
        protocol_version: str,
        manifest: Manifest,
        first_seq: int,
        outbound: SeqCounter | None = None,
    ) -> SessionRecord:
        """Register an accepted session.

        `first_seq` is the `seq` of the body's `hello`, so the next message
        it sends is judged against the counter it actually started from.
        """
        if session in self._records:
            raise KeyError(f"session {session!r} is already open")

        record = SessionRecord(
            session=session,
            body_id=body_id,
            protocol_version=protocol_version,
            manifest=manifest,
            opened_at=now(self._clock),
            inbound=SeqTracker(last=first_seq),
            outbound=outbound if outbound is not None else SeqCounter(),
        )
        self._records[session] = record
        return record

    def close(self, session: str) -> SessionRecord | None:
        return self._records.pop(session, None)

    def get(self, session: str) -> SessionRecord | None:
        return self._records.get(session)

    def receive(
        self,
        session: str,
        message: Message,
        t_received: Timestamp | None = None,
    ) -> Reception:
        """Record an inbound message against its session.

        `t_received` should be stamped by the caller the moment the frame
        arrived, before parsing, so the timestamp measures the wire and not
        how long validation took.
        """
        record = self._require(session)

        stamp = now(self._clock) if t_received is None else t_received
        status, missing = record.inbound.observe(message.seq)

        named = getattr(message, "session", None)
        mismatch = named is not None and named != session

        if status is SeqStatus.GAP:
            log.warning(
                "session %s (body %s): seq gap, %d message(s) skipped before seq %d",
                session,
                record.body_id,
                missing,
                message.seq,
            )
        elif status is SeqStatus.DUPLICATE:
            log.warning(
                "session %s (body %s): seq %d already seen (last was %d), %s repeated",
                session,
                record.body_id,
                message.seq,
                record.inbound.last,
                message.type,
            )

        if mismatch:
            log.warning(
                "session %s (body %s): message names session %r",
                session,
                record.body_id,
                named,
            )

        if isinstance(message, CommandResultEnvelope) and message.payload.is_terminal:
            self.resolve_span(session, message.span_id, message.payload.status)

        return Reception(
            message=message,
            t_received=stamp,
            seq_status=status,
            missing=missing,
            session_mismatch=mismatch,
        )

    # Spans: commands the brain has sent and is waiting on.

    def open_span(self, session: str, command: CommandEnvelope) -> SpanRecord:
        """Record a command as outstanding, from the moment it is sent."""
        record = self._require(session)

        span = SpanRecord(
            span_id=command.span_id,
            trace_id=command.trace_id,
            capability=command.payload.capability,
            action=command.payload.action,
            ttl_ms=command.payload.ttl_ms,
            sent_at=command.ts,
        )
        record.spans[span.span_id] = span
        return span

    def resolve_span(self, session: str, span_id: str, status: str) -> SpanRecord | None:
        """Close a span on its terminal result.

        Returns None when the span is unknown, which means either a result
        for a span this brain never opened or a second terminal result for
        one already closed. SPEC section 6.7 allows exactly one, so both are
        worth seeing.
        """
        record = self._require(session)

        span = record.spans.pop(span_id, None)
        if span is None:
            log.warning(
                "session %s (body %s): terminal %s for unknown or already-closed span %s",
                session,
                record.body_id,
                status,
                span_id,
            )
        return span

    def outstanding(self, session: str) -> tuple[SpanRecord, ...]:
        return tuple(self._require(session).spans.values())

    def mark_lost(self, session: str, *, message: str) -> list[SpanOutcome]:
        """The lease expired: give up on this body and on everything in flight.

        SPEC section 8.1. The body is independently latching `safe_hold` off
        its own lease miss; this is only the brain's side, so that nothing
        upstream waits forever on a result that is never coming.
        """
        record = self._require(session)

        outcomes = [
            SpanOutcome(span=span, status="failed", code=BODY_LOST, message=message)
            for span in record.spans.values()
        ]
        record.spans.clear()

        if not record.lost:
            record.lost = True
            log.warning(
                "session %s (body %s): LOST, %d outstanding span(s) failed",
                session,
                record.body_id,
                len(outcomes),
            )

        return outcomes

    def mark_live(self, session: str) -> bool:
        """A body that was LOST is heartbeating again.

        Not a safety decision and it unlatches nothing: the body stays in
        whatever state it latched itself into until explicitly cleared
        (SPEC section 8.2). This only says the brain can hear it again.
        """
        record = self._require(session)
        if not record.lost:
            return False

        record.lost = False
        log.info("session %s (body %s): heartbeats resumed", session, record.body_id)
        return True

    def _require(self, session: str) -> SessionRecord:
        record = self._records.get(session)
        if record is None:
            raise KeyError(f"session {session!r} is not open")
        return record

    @property
    def sessions(self) -> dict[str, SessionRecord]:
        return dict(self._records)

    def __contains__(self, session: object) -> bool:
        return session in self._records

    def __len__(self) -> int:
        return len(self._records)
