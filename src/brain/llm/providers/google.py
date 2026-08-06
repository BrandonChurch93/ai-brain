"""Google Gemini, through the official `google-genai` SDK.

Verified against google-genai 2.17.0.
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

NAME = "google"

#: Candidate finish reasons that mean the model declined rather than
#: answered. Compared as strings because the SDK returns an enum whose
#: members stringify to these names, and pinning to the enum would make
#: this module import the SDK just to describe a request.
REFUSAL_REASONS = frozenset({"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY"})


class GoogleProvider:
    """Gemini via `generate_content`.

    The system prompt is config here rather than a message, which is the
    one structural difference from the other three: Gemini carries it as
    `system_instruction` alongside the request rather than inside the
    conversation.
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
        config: dict[str, Any] = {"max_output_tokens": prompt.max_tokens}
        if prompt.system is not None:
            config["system_instruction"] = prompt.system

        return {"model": self._model, "contents": prompt.user, "config": config}

    async def send(self, request: dict[str, Any]) -> Completion:
        response = await self._sdk_client().aio.models.generate_content(**request)

        candidates = response.candidates or []
        finish = str(candidates[0].finish_reason) if candidates else None
        # The enum stringifies as "FinishReason.SAFETY"; the name is the
        # part that means anything here.
        reason = finish.rsplit(".", 1)[-1] if finish else None

        usage = response.usage_metadata
        tokens = (
            TokenCount(
                prompt=usage.prompt_token_count,
                completion=usage.candidates_token_count,
                total=usage.total_token_count,
            )
            if usage is not None
            else TokenCount()
        )

        blocked = getattr(response.prompt_feedback, "block_reason", None) is not None

        return Completion(
            text=response.text or "",
            model=response.model_version or self._model,
            provider=NAME,
            tokens=tokens,
            finish_reason=reason,
            status=REFUSAL if blocked or reason in REFUSAL_REASONS else OK,
            raw=response.model_dump(mode="json", exclude_none=True),
        )

    def _sdk_client(self) -> Any:
        if self._client is None:
            try:
                from google import genai
            except ImportError as exc:
                raise LLMDependencyError(
                    "the google-genai SDK is not installed. Provider SDKs are optional "
                    "dependencies so the default install and CI need no API keys: "
                    "uv sync --extra llm"
                ) from exc
            # Unset, the SDK reads GOOGLE_API_KEY then GEMINI_API_KEY.
            self._client = (
                genai.Client() if self._api_key is None else genai.Client(api_key=self._api_key)
            )
        return self._client

    async def aclose(self) -> None:
        client = self._client
        self._client = None
        # The async half of the client owns the connections; the sync
        # `close` on the top-level client is a different door.
        aio = getattr(client, "aio", None)
        if aio is not None and hasattr(aio, "aclose"):
            await aio.aclose()
