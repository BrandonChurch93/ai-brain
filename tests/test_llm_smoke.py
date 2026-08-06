"""The same prompt through every provider, for real (checklist step 5.1).

This is the done-check for the step, and the only thing that proves the
adapters against the actual APIs rather than against fakes shaped like them.
It calls hosted models, so it costs money and needs keys, and it is skipped
unless asked for.

Ask for it one provider at a time, by naming the model to use:

    BRAIN_LLM_SMOKE_ANTHROPIC=claude-opus-5 \\
    BRAIN_LLM_SMOKE_OPENAI=gpt-5 \\
    BRAIN_LLM_SMOKE_GOOGLE=gemini-3-pro \\
    BRAIN_LLM_SMOKE_OLLAMA=llama3.2 \\
    uv run pytest -m llm_smoke

The variable holds a model rather than a `1`, deliberately. A `1` would need
a default model per provider living somewhere in the repo, and ADR-0007
point 3 gives the eval harness sole authority over which model does what. A
default here would become the de facto answer before a single eval had run.

Each hosted provider also needs its own key in the environment, which the
SDKs read themselves: ANTHROPIC_API_KEY, OPENAI_API_KEY, and GOOGLE_API_KEY
(or GEMINI_API_KEY). Ollama needs a daemon on BRAIN_OLLAMA_HOST, or the
local default.

Prerequisites are checked separately from the call. A missing key skips with
the variable named; a call that then fails is a failure, not a skip, because
past that point the adapter is what is being tested.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

from brain.llm import LLMClient, Prompt
from brain.llm import providers as registry
from brain.llm.types import OK

pytestmark = pytest.mark.llm_smoke

#: One sentence, one right answer, and cheap on every provider. Anything
#: longer would be testing the model rather than the adapter.
PROMPT = Prompt(
    user="Reply with the single word: ready",
    system="Answer with one word and no punctuation.",
    max_tokens=1024,
)


@dataclass(frozen=True)
class SmokeCase:
    provider: str
    #: Environment variable holding the model to call.
    variable: str
    #: What the provider needs besides the model. Any one of these being
    #: set is enough; empty means nothing is needed.
    keys: tuple[str, ...]

    @property
    def model(self) -> str | None:
        return os.environ.get(self.variable) or None

    @property
    def missing_key(self) -> str | None:
        if not self.keys or any(os.environ.get(key) for key in self.keys):
            return None
        return " or ".join(self.keys)


CASES = (
    SmokeCase("anthropic", "BRAIN_LLM_SMOKE_ANTHROPIC", ("ANTHROPIC_API_KEY",)),
    SmokeCase("openai", "BRAIN_LLM_SMOKE_OPENAI", ("OPENAI_API_KEY",)),
    SmokeCase("google", "BRAIN_LLM_SMOKE_GOOGLE", ("GOOGLE_API_KEY", "GEMINI_API_KEY")),
    SmokeCase("ollama", "BRAIN_LLM_SMOKE_OLLAMA", ()),
)


class RecordingSink:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def record_llm_io(self, **fields) -> None:
        self.records.append(fields)


@pytest.fixture(params=CASES, ids=lambda case: case.provider)
def case(request) -> SmokeCase:
    smoke: SmokeCase = request.param
    if smoke.model is None:
        pytest.skip(f"set {smoke.variable}=<model> to call {smoke.provider} for real")
    if smoke.missing_key is not None:
        pytest.skip(f"{smoke.provider} needs {smoke.missing_key} in the environment")
    return smoke


async def test_the_same_prompt_runs_through_every_adapter(case: SmokeCase):
    """One prompt, one provider, and the record it leaves behind.

    The assertions are about the adapter, not the model: something came
    back, the tokens were counted, the latency is real, and the whole
    exchange reached the sink. What the model actually said is the eval
    harness's business in step 5.2.
    """
    sink = RecordingSink()
    provider = registry.build(case.provider, case.model)
    client = LLMClient({"classifier": provider}, sink)

    try:
        completion = await client.complete("classifier", PROMPT, trace_id="trc_smoke")
    finally:
        await client.aclose()

    assert completion.status == OK, f"{case.provider} declined: {completion.finish_reason}"
    assert completion.text.strip(), f"{case.provider} returned no text"
    assert completion.provider == case.provider

    assert len(sink.records) == 1
    record = sink.records[0]
    assert record["model"], "the record does not name a model"
    assert record["latency_ms"] > 0
    assert record["trace_id"] == "trc_smoke"
    # ADR-0005 and ADR-0007 point 4: cost per decision, from the first call.
    assert record["tokens"]["prompt"] is not None, f"{case.provider} reported no prompt tokens"
    assert record["tokens"]["completion"] is not None
    assert record["response"] is not None, "the provider's own answer was not kept"


def test_the_suite_names_every_hosted_provider():
    """A provider added without a smoke case would look like a passing run."""
    assert {case.provider for case in CASES} == set(registry.names()) - {"stub"}
