"""The one way to call a model.

Every call is recorded. Not by convention, by construction: the sink is a
required constructor argument, `complete` is the only public way in, and the
recording happens in a `finally` so it survives every exit including the ones
nobody planned. There is no unlogged path to instrument later, because there
was never an unlogged path (ADR-0005).

The routing table is the client's, not the caller's. A caller asks for a
role, never for a model, which is what keeps ADR-0007 point 2 true as models
change underneath.
"""

from __future__ import annotations

import logging
from typing import Any

from brain.llm.types import (
    ERROR,
    Completion,
    LLMSink,
    Prompt,
    Provider,
    RoutingError,
)
from wire.clock import SYSTEM_CLOCK, Clock
from wire.stamp import now

log = logging.getLogger("brain.llm")


class LLMClient:
    """Routes a role to a provider, calls it, and records what happened."""

    def __init__(
        self,
        providers: dict[str, Provider],
        sink: LLMSink,
        *,
        clock: Clock = SYSTEM_CLOCK,
        session: str | None = None,
    ) -> None:
        """`sink` is positional and required.

        A default of `None` would make an unrecorded client the easiest one
        to construct, and the first call written in a hurry would be the one
        that never reaches the log.
        """
        self._providers = dict(providers)
        self._sink = sink
        self._clock = clock
        self._session = session

    @property
    def roles(self) -> tuple[str, ...]:
        """The roles this client can serve, in configured order."""
        return tuple(self._providers)

    def provider_for(self, role: str) -> Provider:
        try:
            return self._providers[role]
        except KeyError:
            raise RoutingError(
                f"no model configured for role {role!r}. "
                f"Configured: {', '.join(self._providers) or 'none'}. "
                f"Set BRAIN_LLM_{role.upper()} to 'provider:model'"
            ) from None

    async def complete(
        self,
        role: str,
        prompt: Prompt,
        *,
        trace_id: str | None = None,
        span_id: str | None = None,
    ) -> Completion:
        """Call the model routed to `role`, and log the exchange.

        `trace_id` and `span_id` are how a decision is tied back to the goal
        that prompted it and the work it caused (ADR-0005 point 3). They are
        optional because the first callers are not inside a span yet, and a
        required id would only get a placeholder invented for it.
        """
        provider = self.provider_for(role)
        request = provider.request(prompt)

        t_request = now(self._clock)
        completion: Completion | None = None
        failure: BaseException | None = None

        try:
            completion = await provider.send(request)
            return completion
        except BaseException as exc:
            # Caught only to name it in the log. Re-raised untouched: a
            # caller telling a rate limit from a bad request needs the
            # provider's own exception type, not a wrapper.
            failure = exc
            raise
        finally:
            t_response = now(self._clock)
            self._record(
                role=role,
                provider=provider,
                request=request,
                completion=completion,
                failure=failure,
                t_request=t_request,
                t_response=t_response,
                trace_id=trace_id,
                span_id=span_id,
            )

    def _record(
        self,
        *,
        role: str,
        provider: Provider,
        request: dict[str, Any],
        completion: Completion | None,
        failure: BaseException | None,
        t_request: Any,
        t_response: Any,
        trace_id: str | None,
        span_id: str | None,
    ) -> None:
        """Write one `llm_io` record for the call that just ended.

        Latency comes off the monotonic clock, so a wall-clock correction
        mid-call cannot produce a negative duration, and an injected clock
        makes it deterministic in tests.
        """
        latency_ms = (t_response.mono_ns - t_request.mono_ns) / 1_000_000

        if completion is not None:
            status = completion.status
            tokens = completion.tokens.as_record()
            response: Any = completion.raw
            error = None
        else:
            status = ERROR
            tokens = {"prompt": None, "completion": None, "total": None}
            response = None
            error = _describe(failure)

        try:
            self._sink.record_llm_io(
                role=role,
                provider=provider.name,
                model=provider.model,
                prompt=request,
                response=response,
                latency_ms=latency_ms,
                t_request=t_request,
                t_response=t_response,
                tokens=tokens,
                session=self._session,
                trace_id=trace_id,
                span_id=span_id,
                status=status,
                error=error,
            )
        except Exception:
            # A recorder that cannot write must not take the brain down with
            # it, and must not quietly swallow the model's answer either.
            # Loudly degraded beats either.
            log.exception("llm_io record for role %s could not be written", role)

    async def aclose(self) -> None:
        for provider in self._providers.values():
            try:
                await provider.aclose()
            except Exception:
                # One provider failing to close must not strand the rest.
                log.exception("closing provider %s failed", provider.name)


def _describe(failure: BaseException | None) -> dict[str, Any] | None:
    """The failure, in a shape the log can hold.

    Type and message rather than a traceback: the traceback belongs to this
    process and this line of code, while the log is read later by something
    that only wants to know what the provider did.
    """
    if failure is None:
        return None
    return {"type": type(failure).__name__, "message": str(failure)}
