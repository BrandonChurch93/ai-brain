"""A provider that answers without a network, a key, or an SDK.

The same argument as `bodies/mock.py` and `StubSpeaker`: the interesting
behaviour is routing, recording, and failure handling, and none of it should
need a paid API call to test. It is also what step 5.4 needs, where the
acceptance test forces the LLM adapter to error and asserts the system stays
responsive: a real provider cannot be told to fail on command.

It is a first-class provider, not test scaffolding: it lives here so a
developer with no keys at all can still run the brain end to end and see
where the model would have been asked.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from brain.llm.types import OK, Completion, Prompt, TokenCount

NAME = "stub"


#: Tokens, if you squint. Enough to make a cost calculation exercise its
#: arithmetic without pretending to be a real tokeniser.
def _count(text: str) -> int:
    return len(text.split())


class StubProvider:
    """Echoes, or does whatever it was told to do.

    `answer` turns a prompt into text. `fail` short-circuits the call with an
    exception, which is how a test drives the error path.
    """

    def __init__(
        self,
        model: str = "stub-1",
        *,
        answer: Callable[[Prompt], str] | str | None = None,
        fail: BaseException | None = None,
        status: str = OK,
    ) -> None:
        self._model = model
        self._answer = answer
        self._fail = fail
        self._status = status
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    @property
    def name(self) -> str:
        return NAME

    @property
    def model(self) -> str:
        return self._model

    def request(self, prompt: Prompt) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt.user}],
            "max_tokens": prompt.max_tokens,
        }
        if prompt.system is not None:
            request["system"] = prompt.system
        return request

    async def send(self, request: dict[str, Any]) -> Completion:
        self.calls.append(request)

        if self._fail is not None:
            raise self._fail

        user = request["messages"][-1]["content"]
        if self._answer is None:
            text = user
        elif isinstance(self._answer, str):
            text = self._answer
        else:
            text = self._answer(Prompt(user=user, system=request.get("system")))

        return Completion(
            text=text,
            model=self._model,
            provider=NAME,
            tokens=TokenCount.summing(_count(user), _count(text)),
            finish_reason="stop",
            status=self._status,
            raw={"model": self._model, "text": text},
        )

    async def aclose(self) -> None:
        self.closed = True
