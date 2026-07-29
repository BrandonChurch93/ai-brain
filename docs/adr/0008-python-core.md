# ADR-0008: Python core for V1

- Status: Proposed
- Date: 2026-07-29

## Context

The brain core needs one implementation language before the first line of code. The author's deepest skill is TypeScript, with strong Python (the prior RAG project was Python end to end). V1's heaviest integrations are AI-ecosystem libraries: whisper.cpp bindings and MLX for STT, Kokoro for TTS, Ollama clients, provider SDKs, the `mcap` library, and `jsonschema`. All are Python-first; several have no serious TypeScript equivalent. A split codebase adds coordination cost a solo V1 cannot justify.

## Decision

The brain core, bodies, and tooling are Python 3.12+, managed with `uv`, linted with `ruff`, tested with `pytest`. TypeScript is reserved for the future dashboard and web surfaces, where it genuinely wins. No TS in V1.

## Consequences

Perception, voice, and logging integrate at library speed instead of through hand-rolled wrappers. The WebSocket layer is slightly less familiar territory than it would be in TS; accepted. Revisit triggers: a performance wall in the fast loop that profiling attributes to Python itself, or the arrival of a web dashboard (which gets TS without reopening this ADR).

## References

- docs/research/middleware-survey.md, sections 5 and 6 (tooling landscape)
