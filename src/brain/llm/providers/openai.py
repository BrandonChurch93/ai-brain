"""OpenAI, through the official `openai` SDK.

Absolute imports mean `import openai` here reaches the installed SDK rather
than this module.

Verified against openai 2.53.0.
"""

from __future__ import annotations

from typing import Any

from brain.llm.types import (
    OK,
    REFUSAL,
    Completion,
    LLMDependencyError,
    Prompt,
    TokenCount,
)

NAME = "openai"

#: `finish_reason` when the response was cut off by a moderation filter.
CONTENT_FILTER = "content_filter"


class OpenAIProvider:
    """Chat Completions, one model per instance.

    `max_completion_tokens` rather than `max_tokens`: the latter is the
    deprecated spelling and is rejected by the reasoning models, which is
    exactly the class of model a planner role would be routed to.
    """

    def __init__(self, model: str, *, api_key: str | None = None, client: Any = None) -> None:
        self._model = model
        self._api_key = api_key
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
            "max_completion_tokens": prompt.max_tokens,
        }

    async def send(self, request: dict[str, Any]) -> Completion:
        response = await self._sdk_client().chat.completions.create(**request)

        choice = response.choices[0]
        # A refusal arrives as a populated `refusal` field with `content`
        # empty, so reading content alone reports an empty answer and loses
        # the fact that the model declined.
        refusal = getattr(choice.message, "refusal", None)
        text = choice.message.content or refusal or ""

        usage = response.usage
        tokens = (
            TokenCount(
                prompt=usage.prompt_tokens,
                completion=usage.completion_tokens,
                total=usage.total_tokens,
            )
            if usage is not None
            else TokenCount()
        )

        declined = refusal is not None or choice.finish_reason == CONTENT_FILTER

        return Completion(
            text=text,
            model=response.model,
            provider=NAME,
            tokens=tokens,
            finish_reason=choice.finish_reason,
            status=REFUSAL if declined else OK,
            raw=response.model_dump(mode="json", exclude_none=True),
        )

    def _sdk_client(self) -> Any:
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:
                raise LLMDependencyError(
                    "the openai SDK is not installed. Provider SDKs are optional "
                    "dependencies so the default install and CI need no API keys: "
                    "uv sync --extra llm"
                ) from exc
            self._client = (
                AsyncOpenAI() if self._api_key is None else AsyncOpenAI(api_key=self._api_key)
            )
        return self._client

    async def aclose(self) -> None:
        client = self._client
        self._client = None
        if client is not None and hasattr(client, "close"):
            await client.close()
