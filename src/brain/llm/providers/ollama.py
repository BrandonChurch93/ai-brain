"""Ollama, through the official `ollama` SDK.

The local provider. ADR-0007 point 2 gives the `reflex` role to something
local and free, and this is it: no API key, no network beyond the host, and
no per-call cost, which is what lets a reflex path stay on the hot loop.

Verified against ollama 0.6.2.
"""

from __future__ import annotations

import os
from typing import Any

from brain.llm.types import (
    OK,
    Completion,
    LLMDependencyError,
    Prompt,
    TokenCount,
)

NAME = "ollama"

#: Where the local daemon listens unless told otherwise. Read from
#: BRAIN_OLLAMA_HOST rather than the SDK's own OLLAMA_HOST so the brain's
#: configuration stays under one prefix (ADR-0000 rule 3).
HOST_ENV = "BRAIN_OLLAMA_HOST"


class OllamaProvider:
    """A model running on this machine.

    No refusal status: a local model has no safety classifier in front of it
    that could decline a request out of band. Whatever it says is its
    answer, and calling some of those answers refusals would be this layer
    guessing at content it does not read.
    """

    def __init__(self, model: str, *, host: str | None = None, client: Any = None) -> None:
        self._model = model
        self._host = host if host is not None else os.environ.get(HOST_ENV)
        self._client = client

    @property
    def name(self) -> str:
        return NAME

    @property
    def model(self) -> str:
        return self._model

    def request(self, prompt: Prompt) -> dict[str, Any]:
        messages: list[dict[str, str]] = []
        if prompt.system is not None:
            messages.append({"role": "system", "content": prompt.system})
        messages.append({"role": "user", "content": prompt.user})

        return {
            "model": self._model,
            "messages": messages,
            "options": {"num_predict": prompt.max_tokens},
        }

    async def send(self, request: dict[str, Any]) -> Completion:
        response = await self._sdk_client().chat(**request)

        return Completion(
            text=response.message.content or "",
            model=response.model or self._model,
            provider=NAME,
            tokens=TokenCount.summing(response.prompt_eval_count, response.eval_count),
            finish_reason=response.done_reason,
            status=OK,
            raw=response.model_dump(mode="json", exclude_none=True),
        )

    def _sdk_client(self) -> Any:
        if self._client is None:
            try:
                from ollama import AsyncClient
            except ImportError as exc:
                raise LLMDependencyError(
                    "the ollama SDK is not installed. Provider SDKs are optional "
                    "dependencies so the default install and CI need no API keys: "
                    "uv sync --extra llm"
                ) from exc
            self._client = AsyncClient(host=self._host)
        return self._client

    async def aclose(self) -> None:
        client = self._client
        self._client = None
        if client is not None and hasattr(client, "close"):
            await client.close()
