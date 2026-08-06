"""The routing table: which model serves which role.

ADR-0007 point 2. Each of the five roles is filled independently, from the
environment, as `provider:model`:

    BRAIN_LLM_PLANNER=anthropic:claude-opus-5
    BRAIN_LLM_CONVERSATION=openai:gpt-5
    BRAIN_LLM_VISION=google:gemini-3-pro
    BRAIN_LLM_CLASSIFIER=anthropic:claude-haiku-4-5
    BRAIN_LLM_REFLEX=ollama:llama3.2

**There are no defaults, on purpose.** ADR-0007 point 3 makes the eval
harness the only authority that assigns models to roles, and says the
initial defaults belong to eval output rather than to a record. A default
written here would be an opinion wearing the eval's clothes, and it would be
the thing everyone actually ran. An unrouted role raises instead, naming the
variable that fills it. Step 5.2 produces the table; step 5.2's 🔶 is where
Brandon sets it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from brain.llm import providers
from brain.llm.client import LLMClient
from brain.llm.types import LLMSink, Provider
from brain.recorder import LLM_ROLES
from wire.clock import SYSTEM_CLOCK, Clock

#: Environment variable for a role's routing entry.
PREFIX = "BRAIN_LLM_"


def variable_for(role: str) -> str:
    return f"{PREFIX}{role.upper()}"


@dataclass(frozen=True, slots=True)
class LLMConfig:
    """A role-to-`provider:model` table, and nothing else.

    Keys are validated against `LLM_ROLES` at construction: a typo in
    `BRAIN_LLM_PLANER` that silently produced an unrouted planner would be
    found at the first mission rather than at startup.
    """

    routes: dict[str, str]

    def __post_init__(self) -> None:
        unknown = sorted(set(self.routes) - set(LLM_ROLES))
        if unknown:
            raise ValueError(
                f"unknown LLM role(s): {', '.join(unknown)}. Known: {', '.join(LLM_ROLES)}"
            )
        # Parsed now so a malformed entry fails at startup rather than on
        # the first call that happens to need that role.
        for spec in self.routes.values():
            providers.parse(spec)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> LLMConfig:
        source = os.environ if env is None else env
        return cls(
            routes={
                role: source[variable_for(role)]
                for role in LLM_ROLES
                if source.get(variable_for(role))
            }
        )

    def build(
        self,
        sink: LLMSink,
        *,
        clock: Clock = SYSTEM_CLOCK,
        session: str | None = None,
    ) -> LLMClient:
        """A client for the configured roles, recording to `sink`."""
        built: dict[str, Provider] = {
            role: providers.from_spec(spec) for role, spec in self.routes.items()
        }
        return LLMClient(built, sink, clock=clock, session=session)
