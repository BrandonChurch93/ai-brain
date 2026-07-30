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

from wire import Manifest, Message, Timestamp
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


class SessionRegistry:
    """Every open session, keyed by session id."""

    def __init__(self) -> None:
        self._records: dict[str, SessionRecord] = {}

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
            opened_at=now(),
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
        record = self._records.get(session)
        if record is None:
            raise KeyError(f"session {session!r} is not open")

        stamp = now() if t_received is None else t_received
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

        return Reception(
            message=message,
            t_received=stamp,
            seq_status=status,
            missing=missing,
            session_mismatch=mismatch,
        )

    @property
    def sessions(self) -> dict[str, SessionRecord]:
        return dict(self._records)

    def __contains__(self, session: object) -> bool:
        return session in self._records

    def __len__(self) -> int:
        return len(self._records)
