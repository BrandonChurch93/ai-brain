"""The provider registry: a name and a model, resolved to an adapter.

Adapter modules are imported lazily. Importing them all up front would be
harmless today, but the point of the optional-extra pattern is that a
machine without an SDK never touches the code that needs it, and a registry
that imports eagerly quietly erodes that.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from brain.llm.types import LLMError, Provider

#: Provider name to the module and class implementing it. Adding a provider
#: is one line here and one module; nothing else in the brain changes, which
#: is the point of ADR-0007.
REGISTRY: dict[str, tuple[str, str]] = {
    "anthropic": ("brain.llm.providers.anthropic", "AnthropicProvider"),
    "openai": ("brain.llm.providers.openai", "OpenAIProvider"),
    "google": ("brain.llm.providers.google", "GoogleProvider"),
    "ollama": ("brain.llm.providers.ollama", "OllamaProvider"),
    "stub": ("brain.llm.providers.stub", "StubProvider"),
}

#: Providers that reach a vendor API. `ollama` is local and `stub` is
#: neither, so neither belongs in a check for "does this need a key".
HOSTED = ("anthropic", "openai", "google")

SEPARATOR = ":"


def names() -> tuple[str, ...]:
    return tuple(REGISTRY)


def build(provider: str, model: str, **options: Any) -> Provider:
    """Construct the adapter for `provider`, bound to `model`."""
    try:
        module_name, class_name = REGISTRY[provider]
    except KeyError:
        raise LLMError(f"unknown provider {provider!r}. Known: {', '.join(REGISTRY)}") from None

    adapter = getattr(import_module(module_name), class_name)
    return adapter(model, **options)


def parse(spec: str) -> tuple[str, str]:
    """Split a `provider:model` routing entry.

    Models have colons in them (`llama3.2:3b` is an ordinary Ollama tag), so
    the split is on the first separator only.
    """
    provider, separator, model = spec.partition(SEPARATOR)
    if not separator or not provider.strip() or not model.strip():
        raise LLMError(
            f"routing entry {spec!r} is not 'provider:model'. Providers: {', '.join(REGISTRY)}"
        )
    return provider.strip(), model.strip()


def from_spec(spec: str, **options: Any) -> Provider:
    provider, model = parse(spec)
    return build(provider, model, **options)
