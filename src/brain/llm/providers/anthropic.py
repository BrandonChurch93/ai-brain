"""Anthropic, through the official `anthropic` SDK.

The module shares a name with the package it imports. That is safe: Python 3
imports are absolute, so `import anthropic` here reaches the installed SDK,
never this file.

Verified against anthropic 0.120.2.
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

NAME = "anthropic"

#: The stop reason the API uses when safety classifiers decline a request.
#: A refusal arrives as a successful HTTP 200 with an empty or partial
#: answer, so a caller that reads the content without checking this sees
#: silence and cannot tell it from a model with nothing to say.
REFUSAL_STOP_REASON = "refusal"


class AnthropicProvider:
    """One model, one client.

    Deliberately minimal: a message, an optional system prompt, a ceiling.
    Anthropic-specific controls (adaptive thinking, effort, structured
    outputs) are real and useful, and they belong to the roles that need
    them rather than to the shared interface. The planner reaches for them
    in step 5.3; a generic caller should not have to know they exist.

    Thinking is left at the model's default, which on current Claude models
    means on. `Prompt.max_tokens` covers thinking and answer together, which
    is why the shared default is generous.
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
        request: dict[str, Any] = {
            "model": self._model,
            "max_tokens": prompt.max_tokens,
            "messages": [{"role": "user", "content": prompt.user}],
        }
        if prompt.system is not None:
            request["system"] = prompt.system
        return request

    async def send(self, request: dict[str, Any]) -> Completion:
        message = await self._sdk_client().messages.create(**request)

        text = "".join(
            block.text for block in message.content if getattr(block, "type", None) == "text"
        )
        usage = message.usage

        return Completion(
            text=text,
            model=message.model,
            provider=NAME,
            tokens=TokenCount.summing(usage.input_tokens, usage.output_tokens),
            finish_reason=message.stop_reason,
            status=REFUSAL if message.stop_reason == REFUSAL_STOP_REASON else OK,
            raw=message.model_dump(mode="json", exclude_none=True),
        )

    def _sdk_client(self) -> Any:
        if self._client is None:
            try:
                from anthropic import AsyncAnthropic
            except ImportError as exc:
                raise LLMDependencyError(
                    "the anthropic SDK is not installed. Provider SDKs are optional "
                    "dependencies so the default install and CI need no API keys: "
                    "uv sync --extra llm"
                ) from exc
            # No api_key means the SDK resolves ANTHROPIC_API_KEY itself,
            # which is one fewer place for a key to be read and logged.
            self._client = (
                AsyncAnthropic() if self._api_key is None else AsyncAnthropic(api_key=self._api_key)
            )
        return self._client

    async def aclose(self) -> None:
        client = self._client
        self._client = None
        if client is not None and hasattr(client, "close"):
            await client.close()
