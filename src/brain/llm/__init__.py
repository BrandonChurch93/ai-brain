"""The provider-agnostic LLM layer (ADR-0007).

Four things, and the boundaries between them are the design:

- `types` is the vocabulary, held at the intersection of what every provider
  can do.
- `providers/` is one adapter per vendor, each importing its SDK lazily so
  the default install and CI need neither the SDK nor an API key.
- `client` is the only way to call a model, and records every call.
- `config` is the role-to-model routing table, from the environment, with no
  defaults: the eval harness assigns models to roles, not this code.

The rule that shapes the rest: the LLM never holds actuator authority
(ADR-0004). Nothing here executes anything. It produces text, which the
validator disposes of.
"""

from brain.llm.client import LLMClient
from brain.llm.config import LLMConfig, variable_for
from brain.llm.types import (
    DEFAULT_MAX_TOKENS,
    ERROR,
    OK,
    REFUSAL,
    Completion,
    LLMDependencyError,
    LLMError,
    LLMSink,
    Prompt,
    Provider,
    RoutingError,
    TokenCount,
)

__all__ = [
    "DEFAULT_MAX_TOKENS",
    "ERROR",
    "OK",
    "REFUSAL",
    "Completion",
    "LLMClient",
    "LLMConfig",
    "LLMDependencyError",
    "LLMError",
    "LLMSink",
    "Prompt",
    "Provider",
    "RoutingError",
    "TokenCount",
    "variable_for",
]
