"""Turning an inbound command into at most one execution and one result.

The order of checks is the safety property. Deduplicate first, then expire,
then execute: a retransmitted command must not run twice even if it is still
within its TTL, and an expired command must not run at all even if it is the
first time it has been seen.
"""

from __future__ import annotations

import asyncio
import inspect
import logging

from bodies.commands import (
    EXPIRED,
    REJECTED,
    CommandLedger,
    Handler,
    Outcome,
    SpanEntry,
)
from wire import (
    CommandEnvelope,
    CommandResultEnvelope,
    CommandResultPayload,
    ErrorDetail,
    without_none,
)
from wire.stamp import new_id, now

log = logging.getLogger("bodies.dispatch")

#: Error codes from SPEC section 6.11, reused on rejected results.
UNKNOWN_CAPABILITY = "unknown_capability"
UNKNOWN_ACTION = "unknown_action"


class CommandDispatcher:
    """Routes commands to handlers, enforcing the section 6.6 and 6.7 rules."""

    def __init__(self, client, ledger: CommandLedger) -> None:
        self._client = client
        self._ledger = ledger
        self._handlers: dict[tuple[str, str], Handler] = {}

    def on(self, capability: str, action: str, handler: Handler) -> None:
        self._handlers[(capability, action)] = handler

    @property
    def ledger(self) -> CommandLedger:
        return self._ledger

    async def handle(self, command: CommandEnvelope, received_at=None) -> SpanEntry | None:
        """Process one command. Returns the span, or None if it was a duplicate."""
        stamp = received_at if received_at is not None else now(self._client.clock)

        entry = self._ledger.admit(command, stamp)
        if entry is None:
            # A retransmission. Not executed, and deliberately not answered
            # again: the original result was the outcome, and re-sending it
            # would be a second terminal result for one span, which SPEC
            # section 6.7 forbids.
            return None

        if self._ledger.expired(entry):
            log.warning(
                "span %s (%s/%s) expired %.0fms before it could begin; not executing",
                entry.span_id,
                entry.capability,
                entry.action,
                -self._ledger.remaining_ms(entry),
            )
            await self._finish(
                command,
                entry,
                Outcome(
                    status=EXPIRED,
                    code="ttl_expired",
                    message=f"not begun within {entry.ttl_ms}ms of receipt",
                ),
            )
            return entry

        handler = self._handlers.get((entry.capability, entry.action))
        if handler is None:
            await self._finish(command, entry, self._no_handler(entry))
            return entry

        self._ledger.start(entry)
        try:
            result = handler(command)
            outcome = await result if inspect.isawaitable(result) else result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("span %s (%s/%s) raised", entry.span_id, entry.capability, entry.action)
            outcome = Outcome(status="failed", code="internal", message=str(exc))

        await self._finish(command, entry, outcome)
        return entry

    def _no_handler(self, entry: SpanEntry) -> Outcome:
        """Refuse cleanly, naming which half was wrong.

        The brain's validator grounds commands against the manifest before
        sending (ADR-0004), so reaching here means either a bug there or a
        manifest this body does not honour. Either is worth naming precisely.
        """
        known = {capability for capability, _ in self._handlers}
        if entry.capability not in known:
            return Outcome(
                status=REJECTED,
                code=UNKNOWN_CAPABILITY,
                message=f"no capability {entry.capability!r} on this body",
            )
        return Outcome(
            status=REJECTED,
            code=UNKNOWN_ACTION,
            message=f"capability {entry.capability!r} has no action {entry.action!r}",
        )

    async def finish_span(self, entry: SpanEntry, outcome: Outcome) -> bool:
        """End a span from outside a handler, as a latch does (section 8.1).

        Returns False when the span had already ended, in which case nothing
        is sent: exactly one terminal result per span holds no matter who is
        trying to end it.
        """
        return await self._emit_terminal(entry, outcome, trace_id=entry.trace_id)

    async def _finish(self, command: CommandEnvelope, entry: SpanEntry, outcome: Outcome) -> None:
        """Send the single terminal result for this span."""
        await self._emit_terminal(entry, outcome, trace_id=command.trace_id)

    async def _emit_terminal(
        self, entry: SpanEntry, outcome: Outcome, *, trace_id: str | None
    ) -> bool:
        if not self._ledger.complete(entry, outcome.status):
            return False  # already ended; the ledger has logged it

        if not entry.expects_result:
            return True

        error = (
            ErrorDetail(code=outcome.code, message=outcome.message or "")
            if outcome.code is not None
            else None
        )

        # The span has already ended locally. Announcing it is a courtesy the
        # network may refuse, and a body that latched because the socket died
        # must not fail to end its spans for want of that same socket.
        try:
            await self._send_result(entry, outcome, trace_id, error)
        except Exception as exc:
            log.warning(
                "span %s ended %s but the result could not be sent: %s",
                entry.span_id,
                outcome.status,
                exc,
            )
        return True

    async def _send_result(self, entry, outcome, trace_id, error) -> None:
        await self._client.send(
            CommandResultEnvelope(
                **without_none(
                    type="command_result",
                    id=new_id(),
                    session=self._client.session,
                    seq=self._client.next_seq(),
                    ts=now(self._client.clock),
                    trace_id=trace_id,
                    span_id=entry.span_id,
                    payload=CommandResultPayload(
                        **without_none(
                            status=outcome.status,
                            progress=1.0,
                            data=outcome.data,
                            error=error,
                        )
                    ),
                )
            )
        )
