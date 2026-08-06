"""The LLM layer, offline.

Nothing here needs an SDK, an API key, or a network. That is the point of
the split: the routing, recording, and failure behaviour is the part that
must never regress, and it should cost nothing to check on every push.

The provider adapters are exercised against fake SDK clients. Those fakes
encode response shapes read off the installed SDKs (anthropic 0.120.2,
openai 2.53.0, google-genai 2.17.0, ollama 0.6.2), which means they can go
stale when a vendor renames a field. `test_llm_smoke.py` is what proves them
against the real thing; this file proves the mapping logic around them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from brain.llm import (
    ERROR,
    OK,
    REFUSAL,
    Completion,
    LLMClient,
    LLMConfig,
    LLMDependencyError,
    LLMError,
    Prompt,
    Provider,
    RoutingError,
    TokenCount,
    variable_for,
)
from brain.llm import providers as registry
from brain.llm.providers.anthropic import AnthropicProvider
from brain.llm.providers.google import GoogleProvider
from brain.llm.providers.ollama import OllamaProvider
from brain.llm.providers.openai import OpenAIProvider
from brain.llm.providers.stub import StubProvider
from brain.recorder import LLM_IO_SCHEMA, LLM_ROLES
from wire.clock import ManualClock

PROMPT = Prompt(user="Say the word ready.", system="You are terse.", max_tokens=64)


class RecordingSink:
    """An `LLMSink` that keeps records in a list.

    Same call signature as the flight recorder, so a test that passes here
    is calling the recorder correctly too.
    """

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def record_llm_io(self, **fields: Any) -> None:
        self.records.append(fields)

    @property
    def only(self) -> dict[str, Any]:
        assert len(self.records) == 1, f"expected one record, got {len(self.records)}"
        return self.records[0]


def client_for(provider: Provider, *, role: str = "planner", clock: ManualClock | None = None):
    sink = RecordingSink()
    ticking = clock if clock is not None else ManualClock()
    return LLMClient({role: provider}, sink, clock=ticking), sink, ticking


# --------------------------------------------------------------------------
# Recording is not optional
# --------------------------------------------------------------------------


async def test_a_successful_call_is_recorded_once():
    client, sink, _ = client_for(StubProvider(answer="ready"))

    completion = await client.complete("planner", PROMPT, trace_id="trc_1", span_id="spn_1")

    assert completion.text == "ready"
    record = sink.only
    assert record["role"] == "planner"
    assert record["provider"] == "stub"
    assert record["model"] == "stub-1"
    assert record["status"] == OK
    assert record["trace_id"] == "trc_1"
    assert record["span_id"] == "spn_1"
    assert record["tokens"]["prompt"] is not None
    assert record["tokens"]["completion"] is not None


async def test_a_failing_call_is_still_recorded():
    """The path that matters most is the one with nothing to return.

    A layer that logs only successes answers "what did it say?" and never
    "why did it stop?", which is the question ADR-0005 exists for.
    """
    boom = TimeoutError("provider went away")
    client, sink, _ = client_for(StubProvider(fail=boom))

    with pytest.raises(TimeoutError):
        await client.complete("planner", PROMPT)

    record = sink.only
    assert record["status"] == ERROR
    assert record["response"] is None
    assert record["error"] == {"type": "TimeoutError", "message": "provider went away"}
    # The request is still there: what was asked is knowable even though
    # nothing came back.
    assert record["prompt"]["messages"][-1]["content"] == PROMPT.user


async def test_a_cancelled_call_is_recorded():
    """Cancellation is an outcome, not an absence.

    Recording in a `finally` rather than an `except Exception` is what makes
    this true, and a spun-down mission is exactly when the log matters.
    """
    import asyncio

    client, sink, _ = client_for(StubProvider(fail=asyncio.CancelledError()))

    with pytest.raises(asyncio.CancelledError):
        await client.complete("planner", PROMPT)

    assert sink.only["error"]["type"] == "CancelledError"


async def test_a_refusal_records_as_a_refusal_not_an_error():
    client, sink, _ = client_for(StubProvider(answer="I cannot help with that", status=REFUSAL))

    completion = await client.complete("planner", PROMPT)

    assert completion.status == REFUSAL
    assert sink.only["status"] == REFUSAL
    # Not an error: nothing failed. The model was asked and declined, which
    # is a real answer and the validator's problem, not the transport's.
    assert sink.only["error"] is None


async def test_latency_comes_off_the_injected_clock():
    """No wall-clock sleeping in the suite, and no flaky timing assertion."""
    clock = ManualClock()

    class Slow(StubProvider):
        async def send(self, request):
            clock.advance(ms=250)
            return await super().send(request)

    client, sink, _ = client_for(Slow(), clock=clock)

    await client.complete("planner", PROMPT)

    assert sink.only["latency_ms"] == pytest.approx(250.0)


async def test_a_broken_recorder_does_not_break_the_call(caplog):
    """Degraded loudly beats losing the answer."""

    class Broken:
        def record_llm_io(self, **fields: Any) -> None:
            raise OSError("disk full")

    client = LLMClient({"planner": StubProvider(answer="ready")}, Broken(), clock=ManualClock())

    completion = await client.complete("planner", PROMPT)

    assert completion.text == "ready"
    assert "llm_io record" in caplog.text


async def test_recorded_fields_satisfy_the_llm_io_schema():
    """The sink is not a different shape from the channel it writes to."""
    import jsonschema

    from wire.stamp import now

    client, sink, clock = client_for(StubProvider(answer="ready"))
    await client.complete("planner", PROMPT)

    fields = sink.only
    stamp = now(clock)
    record = {
        "index": 0,
        "role": fields["role"],
        "provider": fields["provider"],
        "model": fields["model"],
        "prompt": fields["prompt"],
        "response": fields["response"],
        "tokens": fields["tokens"],
        "latency_ms": fields["latency_ms"],
        "t_request": {"mono_ns": stamp.mono_ns, "utc": stamp.utc},
        "t_response": {"mono_ns": stamp.mono_ns, "utc": stamp.utc},
        "status": fields["status"],
        "error": fields["error"],
    }
    jsonschema.validate(record, LLM_IO_SCHEMA)
    # And it survives the trip to disk the recorder will make.
    json.dumps(record)


async def test_it_reaches_the_real_flight_recorder(tmp_path):
    """`FlightRecorder` satisfies `LLMSink` structurally, not by assertion."""
    from mcap.reader import make_reader

    from brain.recorder import LLM_IO_TOPIC, FlightRecorder

    path = tmp_path / "llm.mcap"
    recorder = FlightRecorder(path)
    recorder.open()
    client = LLMClient({"planner": StubProvider(answer="ready")}, recorder, clock=ManualClock())

    await client.complete("planner", PROMPT, trace_id="trc_1")

    recorder.close()

    with path.open("rb") as handle:
        logged = [
            json.loads(message.data)
            for _, channel, message in make_reader(handle).iter_messages()
            if channel.topic == LLM_IO_TOPIC
        ]

    assert len(logged) == 1
    assert logged[0]["role"] == "planner"
    assert logged[0]["trace_id"] == "trc_1"
    assert logged[0]["tokens"]["total"] is not None


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------


async def test_an_unrouted_role_names_the_variable_that_fills_it():
    client, _, _ = client_for(StubProvider(), role="planner")

    with pytest.raises(RoutingError, match="BRAIN_LLM_VISION"):
        await client.complete("vision", PROMPT)


def test_the_routing_table_has_no_defaults():
    """ADR-0007 point 3: evals assign models to roles, not this code."""
    assert LLMConfig.from_env(env={}).routes == {}


def test_routing_reads_one_variable_per_role():
    env = {variable_for(role): f"stub:{role}-model" for role in LLM_ROLES}

    config = LLMConfig.from_env(env=env)

    assert set(config.routes) == set(LLM_ROLES)
    assert config.routes["planner"] == "stub:planner-model"


def test_an_unknown_role_is_rejected_at_construction():
    with pytest.raises(ValueError, match="planer"):
        LLMConfig(routes={"planer": "stub:x"})


def test_a_malformed_entry_fails_at_startup_not_at_first_call():
    with pytest.raises(LLMError, match="provider:model"):
        LLMConfig(routes={"planner": "claude-opus-5"})


def test_a_model_tag_may_contain_colons():
    assert registry.parse("ollama:llama3.2:3b") == ("ollama", "llama3.2:3b")


def test_an_unknown_provider_names_the_known_ones():
    with pytest.raises(LLMError, match="anthropic"):
        registry.build("acme", "acme-1")


def test_config_builds_a_recording_client():
    sink = RecordingSink()
    config = LLMConfig(routes={"planner": "stub:stub-1", "reflex": "stub:stub-1"})

    client = config.build(sink)

    assert client.roles == ("planner", "reflex")


# --------------------------------------------------------------------------
# Provider adapters: the request each one would send
# --------------------------------------------------------------------------


@dataclass
class AdapterCase:
    """One provider adapter under test.

    `response` is a fake SDK response object shaped like the real one, so
    the mapping from vendor fields to `Completion` is exercised without a
    network call.
    """

    name: str
    build: Any
    response: Any
    expected_text: str
    expected_tokens: TokenCount
    extra: dict[str, Any] = field(default_factory=dict)


class FakeResponse:
    """Anything a `model_dump(mode=..., exclude_none=...)` is called on."""

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return {"faked": True}


class FakeAnthropicMessage(FakeResponse):
    def __init__(self, stop_reason: str = "end_turn") -> None:
        self.content = [_Block("text", "ready"), _Block("thinking", "")]
        self.model = "claude-opus-5"
        self.stop_reason = stop_reason
        self.usage = _Simple(input_tokens=11, output_tokens=2)


class _Block:
    def __init__(self, type_: str, text: str) -> None:
        self.type = type_
        self.text = text


class _Simple:
    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)


class FakeOpenAICompletion(FakeResponse):
    def __init__(self, refusal: str | None = None, finish_reason: str = "stop") -> None:
        message = _Simple(content=None if refusal else "ready", refusal=refusal)
        self.choices = [_Simple(message=message, finish_reason=finish_reason)]
        self.model = "gpt-5"
        self.usage = _Simple(prompt_tokens=11, completion_tokens=2, total_tokens=13)


class FakeGoogleResponse(FakeResponse):
    def __init__(self, finish_reason: str = "FinishReason.STOP") -> None:
        self.text = "ready"
        self.candidates = [_Simple(finish_reason=finish_reason)]
        self.model_version = "gemini-3-pro"
        self.prompt_feedback = None
        self.usage_metadata = _Simple(
            prompt_token_count=11, candidates_token_count=2, total_token_count=13
        )


class FakeOllamaResponse(FakeResponse):
    def __init__(self) -> None:
        self.message = _Simple(content="ready")
        self.model = "llama3.2"
        self.done_reason = "stop"
        self.prompt_eval_count = 11
        self.eval_count = 2


class FakeSDKClient:
    """Stands in for a vendor's async client.

    Each SDK is reached by a different attribute path, so the fake wires up
    every one of them and records the kwargs it was handed. Which path an
    adapter actually walks is part of what the test is checking.
    """

    def __init__(self, response: Any) -> None:
        self._response = response
        self.sent: dict[str, Any] = {}
        self.closed = False
        # anthropic: client.messages.create(...)
        self.messages = _Simple(create=self._call)
        # openai: client.chat.completions.create(...)
        self.chat = _Simple(completions=_Simple(create=self._call))
        # google: client.aio.models.generate_content(...) / client.aio.aclose()
        self.aio = _Simple(models=_Simple(generate_content=self._call), aclose=self._close)

    async def _call(self, **kwargs: Any) -> Any:
        self.sent = kwargs
        return self._response

    async def close(self) -> None:
        await self._close()

    async def _close(self) -> None:
        self.closed = True


class FakeOllamaClient(FakeSDKClient):
    """ollama: client.chat(...) directly, so it displaces the openai path."""

    def __init__(self, response: Any) -> None:
        super().__init__(response)
        self.chat = self._call


CASES = [
    AdapterCase(
        name="anthropic",
        build=lambda client: AnthropicProvider("claude-opus-5", client=client),
        response=FakeAnthropicMessage(),
        expected_text="ready",
        expected_tokens=TokenCount(11, 2, 13),
    ),
    AdapterCase(
        name="openai",
        build=lambda client: OpenAIProvider("gpt-5", client=client),
        response=FakeOpenAICompletion(),
        expected_text="ready",
        expected_tokens=TokenCount(11, 2, 13),
    ),
    AdapterCase(
        name="google",
        build=lambda client: GoogleProvider("gemini-3-pro", client=client),
        response=FakeGoogleResponse(),
        expected_text="ready",
        expected_tokens=TokenCount(11, 2, 13),
    ),
    AdapterCase(
        name="ollama",
        build=lambda client: OllamaProvider("llama3.2", client=client),
        response=FakeOllamaResponse(),
        expected_text="ready",
        expected_tokens=TokenCount(11, 2, 13),
    ),
]

BY_NAME = {case.name: case for case in CASES}


@pytest.fixture(params=[case.name for case in CASES])
def case(request) -> AdapterCase:
    return BY_NAME[request.param]


def fake_client_for(case: AdapterCase) -> FakeSDKClient:
    fake = FakeOllamaClient if case.name == "ollama" else FakeSDKClient
    return fake(case.response)


def test_every_provider_in_the_registry_has_a_case():
    """Adding a provider without a test is caught here, not in production."""
    assert set(BY_NAME) | {"stub"} == set(registry.names())


def test_the_request_carries_the_model_and_the_prompt(case: AdapterCase):
    provider = case.build(None)

    request = provider.request(PROMPT)

    assert request["model"] == provider.model
    assert PROMPT.user in json.dumps(request)
    assert PROMPT.system in json.dumps(request)
    # And it is loggable: the flight recorder writes JSON.
    json.dumps(request)


def test_the_request_needs_no_sdk(case: AdapterCase):
    """`request` is pure, which is what lets a failed call still be logged."""
    provider = case.build(None)
    assert provider.request(PROMPT)["model"] == provider.model


async def test_the_response_maps_to_a_completion(case: AdapterCase):
    fake = fake_client_for(case)
    provider = case.build(fake)

    completion = await provider.send(provider.request(PROMPT))

    assert completion.text == case.expected_text
    assert completion.provider == case.name
    assert completion.tokens == case.expected_tokens
    assert completion.status == OK
    assert completion.raw == {"faked": True}


async def test_the_request_reaches_the_sdk_unchanged(case: AdapterCase):
    fake = fake_client_for(case)
    provider = case.build(fake)
    request = provider.request(PROMPT)

    await provider.send(request)

    assert fake.sent == request


async def test_closing_closes_the_sdk_client(case: AdapterCase):
    fake = fake_client_for(case)
    provider = case.build(fake)

    await provider.aclose()

    assert fake.closed


async def test_anthropic_reports_a_refusal():
    declined = FakeSDKClient(FakeAnthropicMessage("refusal"))
    provider = AnthropicProvider("claude-opus-5", client=declined)

    completion = await provider.send(provider.request(PROMPT))

    assert completion.status == REFUSAL


async def test_openai_reports_a_refusal_and_keeps_its_text():
    """`content` is empty on a refusal; the reason lives in `refusal`."""
    response = FakeOpenAICompletion(refusal="I cannot help with that")
    provider = OpenAIProvider("gpt-5", client=FakeSDKClient(response))

    completion = await provider.send(provider.request(PROMPT))

    assert completion.status == REFUSAL
    assert completion.text == "I cannot help with that"


async def test_google_reports_a_safety_stop_as_a_refusal():
    provider = GoogleProvider(
        "gemini-3-pro", client=FakeSDKClient(FakeGoogleResponse("FinishReason.SAFETY"))
    )

    completion = await provider.send(provider.request(PROMPT))

    assert completion.status == REFUSAL
    assert completion.finish_reason == "SAFETY"


# --------------------------------------------------------------------------
# The optional-extra contract
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "module", "extra"),
    [
        ("anthropic", "anthropic", "anthropic"),
        ("openai", "openai", "openai"),
        ("google", "google.genai", "google-genai"),
        ("ollama", "ollama", "ollama"),
    ],
)
async def test_a_missing_sdk_names_the_extra(monkeypatch, provider, module, extra):
    """The error a fresh checkout gets must say what to install.

    Simulated rather than observed, because a machine that has the SDK
    installed cannot un-install it for one test. The import is blocked, and
    the message is asserted to name both the package and the extra.
    """
    import builtins

    real_import = builtins.__import__
    blocked = module.split(".")[0]

    def refuse(name, *args, **kwargs):
        if name == module or name.split(".")[0] == blocked:
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)

    adapter = registry.build(provider, "some-model")
    with pytest.raises(LLMDependencyError) as caught:
        await adapter.send(adapter.request(PROMPT))

    assert extra in str(caught.value)
    assert "uv sync --extra llm" in str(caught.value)


def test_hosted_providers_are_the_ones_that_need_a_key():
    """`ollama` is local and `stub` is nothing; neither takes a key."""
    assert set(registry.HOSTED) == set(registry.names()) - {"ollama", "stub"}


# --------------------------------------------------------------------------
# Shared vocabulary
# --------------------------------------------------------------------------


def test_a_provider_that_reports_only_the_parts_gets_a_total():
    assert TokenCount.summing(11, 2).total == 13


def test_an_unreported_count_stays_unreported():
    """`None` means the provider did not say. Zero would be a claim."""
    assert TokenCount.summing(None, 2).total is None
    assert TokenCount().as_record() == {"prompt": None, "completion": None, "total": None}


def test_the_shared_prompt_carries_no_sampling_knobs():
    """`temperature` is accepted by three providers and 400s on the fourth.

    A field that silently means nothing on one provider and breaks another
    is worse than no field, so steering lives in the prompt text.
    """
    assert not hasattr(Prompt(user="x"), "temperature")


def test_completion_defaults_to_ok_with_no_tokens():
    completion = Completion(text="x", model="m", provider="p")
    assert completion.status == OK
    assert completion.tokens.as_record()["total"] is None
