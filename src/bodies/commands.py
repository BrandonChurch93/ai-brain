"""Body-side command semantics: TTL, deduplication, one terminal per span.

Three rules from SPEC sections 6.6 and 6.7, all enforced here rather than in
each adapter, so a new body inherits them instead of reimplementing them.

TTL is measured from the body's own receipt time, not from the sender's
timestamp. Clocks between devices differ by tens of milliseconds and the
skew is not knowable, so a lifespan computed against a remote clock is a
lifespan that might already be wrong. Measuring locally is what makes a
stalled network inherently safe.

Deduplication is by `span_id` within a session: a retransmitted command
executes at most once.

Exactly one terminal result per span. A span that has ended cannot end
again, whatever arrives afterwards.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from wire import CommandEnvelope, Timestamp
from wire.clock import SYSTEM_CLOCK, Clock
from wire.models import TERMINAL_STATUSES

log = logging.getLogger("bodies.commands")

#: Terminal statuses a body produces (SPEC section 6.7).
SUCCEEDED = "succeeded"
FAILED = "failed"
EXPIRED = "expired"
REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class Outcome:
    """What a handler decided. Terminal by construction."""

    status: str = SUCCEEDED
    data: dict[str, Any] = field(default_factory=dict)
    code: str | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        if self.status not in TERMINAL_STATUSES:
            raise ValueError(f"{self.status!r} is not a terminal status")


def succeeded(**data: Any) -> Outcome:
    return Outcome(status=SUCCEEDED, data=data)


def failed(code: str, message: str) -> Outcome:
    return Outcome(status=FAILED, code=code, message=message)


def rejected(code: str, message: str) -> Outcome:
    return Outcome(status=REJECTED, code=code, message=message)


@dataclass(slots=True)
class SpanEntry:
    """One command this body has seen, from receipt to terminal result."""

    span_id: str
    trace_id: str | None
    capability: str
    action: str
    ttl_ms: int
    received_at: Timestamp
    received_mono_ns: int
    expects_result: bool = True
    started: bool = False
    terminal: str | None = None

    @property
    def deadline_ns(self) -> int:
        return self.received_mono_ns + self.ttl_ms * 1_000_000

    @property
    def is_terminal(self) -> bool:
        return self.terminal is not None


class CommandLedger:
    """Every command seen this session, and what became of it.

    Session-scoped, like `seq`. A reconnect is a new session and therefore a
    clean ledger, which is correct: `span_id` uniqueness is only promised
    within a session (SPEC section 6.6).
    """

    def __init__(self, clock: Clock = SYSTEM_CLOCK) -> None:
        self._clock = clock
        self._spans: dict[str, SpanEntry] = {}

    def __len__(self) -> int:
        return len(self._spans)

    def get(self, span_id: str) -> SpanEntry | None:
        return self._spans.get(span_id)

    def admit(self, command: CommandEnvelope, received_at: Timestamp) -> SpanEntry | None:
        """Record a newly arrived command, or None if it is a retransmission.

        `received_at` is stamped by the caller the moment the frame arrived,
        so the TTL clock starts at the wire and not after parsing.
        """
        existing = self._spans.get(command.span_id)
        if existing is not None:
            # Suppressed, and deliberately not answered again. If result
            # recovery is ever genuinely needed, the answer is an additive
            # span-status query message with its own ADR, never an idempotent
            # re-send carved out of the exactly-one-terminal-result rule. One
            # exception to that rule and a brain can no longer tell which
            # outcome was real.
            log.info(
                "span %s already seen (%s/%s, %s); not executing again",
                command.span_id,
                existing.capability,
                existing.action,
                existing.terminal or "still running",
            )
            return None

        entry = SpanEntry(
            span_id=command.span_id,
            trace_id=command.trace_id,
            capability=command.payload.capability,
            action=command.payload.action,
            ttl_ms=command.payload.ttl_ms,
            received_at=received_at,
            received_mono_ns=received_at.mono_ns,
            expects_result=command.payload.expects_result,
        )
        self._spans[entry.span_id] = entry
        return entry

    def expired(self, entry: SpanEntry) -> bool:
        """Has this command's lifespan run out before it began?

        Checked at the moment execution would start. A command already
        running is not killed by its TTL: SPEC section 6.6 is about not
        *beginning* late, because starting an action whose context has gone
        stale is the dangerous case.
        """
        if entry.started:
            return False
        return self._clock.mono_ns() >= entry.deadline_ns

    def remaining_ms(self, entry: SpanEntry) -> float:
        return (entry.deadline_ns - self._clock.mono_ns()) / 1_000_000

    def start(self, entry: SpanEntry) -> None:
        entry.started = True

    def complete(self, entry: SpanEntry, status: str) -> bool:
        """Close a span. False if it was already closed.

        The caller must not send a result when this returns False: SPEC
        section 6.7 allows exactly one terminal result per span, and a second
        would leave the brain unable to say which outcome was real.
        """
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"{status!r} is not a terminal status")

        if entry.terminal is not None:
            log.warning(
                "span %s already ended as %s; refusing to end it again as %s",
                entry.span_id,
                entry.terminal,
                status,
            )
            return False

        entry.terminal = status
        return True

    def outstanding(self) -> tuple[SpanEntry, ...]:
        """Spans admitted and not yet terminal."""
        return tuple(entry for entry in self._spans.values() if not entry.is_terminal)


#: What an adapter provides per (capability, action).
Handler = Callable[[CommandEnvelope], Awaitable[Outcome] | Outcome]
