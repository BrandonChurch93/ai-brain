"""The provider-agnostic vocabulary: what a call is, what comes back.

ADR-0007 point 1: one internal interface abstracts all providers. That only
holds if the shared types stay at the intersection of what every provider
can do. Anything one vendor supports and another does not belongs in that
adapter, not here.

The clearest current example is sampling. `temperature` is accepted by
OpenAI, Google, and Ollama, and rejected outright by the newest Anthropic
models, which return 400 for it. A `temperature` field on `Prompt` would
therefore be a field that silently means nothing on one provider and breaks
another, which is worse than not having it. Steering goes in the prompt
text, which every provider reads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from wire.models import Timestamp

#: A generous default. The reason is Anthropic-shaped but the cost of being
#: wrong is universal: on current Claude models thinking is on unless asked
#: otherwise, and `max_tokens` caps thinking and answer together, so a tight
#: budget can spend itself reasoning and truncate mid-sentence. Every other
#: provider treats a ceiling it does not reach as free.
DEFAULT_MAX_TOKENS = 4096

#: What `status` can be on an `llm_io` record.
OK = "ok"
ERROR = "error"
REFUSAL = "refusal"


class LLMError(RuntimeError):
    """Something in the LLM layer itself went wrong.

    Provider API failures are not this: they propagate as the SDK's own
    exception type so callers can tell a rate limit from a bad request
    without unwrapping. This covers the layer's own faults, such as a role
    with no model behind it.
    """


class LLMDependencyError(LLMError):
    """A provider SDK is not installed.

    Same shape as the capture hardware in `bodies/`: the SDKs are optional
    extras so the default install and CI need neither them nor an API key.
    The message names the extra that fixes it.
    """


class RoutingError(LLMError):
    """No model is configured for this role."""


@dataclass(frozen=True, slots=True)
class Prompt:
    """One exchange, in the terms every provider understands.

    Single turn on purpose. Conversation history is the caller's business
    in v1, and a multi-turn type invented before anything needs it would be
    guesswork frozen into an interface.
    """

    user: str
    system: str | None = None
    max_tokens: int = DEFAULT_MAX_TOKENS


@dataclass(frozen=True, slots=True)
class TokenCount:
    """Cost telemetry for one call (ADR-0007 point 4).

    Every field is optional because not every provider reports every count:
    some give prompt and completion and no total, some the reverse. A `None`
    means the provider did not say, which is a different fact from zero and
    is kept as such.
    """

    prompt: int | None = None
    completion: int | None = None
    total: int | None = None

    def as_record(self) -> dict[str, int | None]:
        return {"prompt": self.prompt, "completion": self.completion, "total": self.total}

    @staticmethod
    def summing(prompt: int | None, completion: int | None) -> TokenCount:
        """Fill in `total` when the provider reported only the parts."""
        total = None if prompt is None or completion is None else prompt + completion
        return TokenCount(prompt=prompt, completion=completion, total=total)


@dataclass(frozen=True, slots=True)
class Completion:
    """What came back, normalised, with the provider's own answer kept whole.

    `raw` is the entire response as the provider sent it. ADR-0005 exists so
    "why did it do that?" is answerable later, and normalised text alone
    cannot answer it: the reason a model stopped, or refused, or spent the
    tokens it did, lives in fields this layer does not model.
    """

    text: str
    model: str
    provider: str
    tokens: TokenCount = field(default_factory=TokenCount)
    finish_reason: str | None = None
    status: str = OK
    raw: Any = None


@runtime_checkable
class Provider(Protocol):
    """One vendor's API, reduced to two steps.

    Split deliberately. `request` is pure: it turns a `Prompt` into the
    body this provider would send, needs no network and no SDK, and is what
    the flight recorder logs as the prompt. `send` performs the call.

    Because the request exists before the call does, a call that fails still
    has something truthful to record. A single `complete(prompt)` method
    would leave the log with nothing but an exception on the paths that
    matter most.

    `send` is not the entry point. Everything goes through `LLMClient`,
    which is what makes the recording unskippable.
    """

    @property
    def name(self) -> str: ...

    @property
    def model(self) -> str: ...

    def request(self, prompt: Prompt) -> dict[str, Any]: ...

    async def send(self, request: dict[str, Any]) -> Completion: ...

    async def aclose(self) -> None: ...


class LLMSink(Protocol):
    """Where exchanges are recorded.

    Structurally satisfied by `brain.recorder.FlightRecorder`, which is what
    runs in production. Naming it as a protocol rather than importing the
    recorder keeps the LLM layer testable without an MCAP file, and keeps
    the dependency pointing one way.
    """

    def record_llm_io(
        self,
        *,
        role: str,
        provider: str,
        model: str,
        prompt: Any,
        response: Any,
        latency_ms: float,
        t_request: Timestamp,
        t_response: Timestamp,
        tokens: dict[str, int | None] | None = None,
        session: str | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
        status: str = OK,
        error: dict[str, Any] | None = None,
    ) -> None: ...
