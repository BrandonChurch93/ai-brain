# ADR-0007: Provider-agnostic LLM layer, model-per-role, evals decide

- Status: Accepted
- Date: 2026-07-29

## Context

The model landscape shifts monthly, the three frontier labs now sit within a few points of each other on commonly cited benchmarks, and every vendor recommendation (including recommendations from an AI assistant made by a vendor) carries potential bias. The brain also has genuinely different LLM roles with different requirements: plan decomposition wants tool-use reliability, vision narration wants multimodal strength, classification wants speed and price, reflex work wants local and free.

## Decision

The brain never hardcodes a model or provider.

1. One internal LLM interface abstracts all providers.
2. Config defines a routing table of model-per-role: `planner`, `conversation`, `vision`, `classifier`, `reflex` (local). Each slot is independently fillable and swappable.
3. A small eval harness of real brain tasks (plan decomposition, capability grounding, structured-output validity, refusal of infeasible commands) is the only authority that assigns models to roles. It runs at build time to set initial defaults and reruns when candidate models release.
4. Cost telemetry (tokens per decision, per role) is logged per ADR-0005, so routing decisions can weigh price with evidence.

## Consequences

No provider lock-in, and the routing table becomes a living record of measured best-fit rather than opinion. The harness is permanent infrastructure, a pattern already proven in the author's prior RAG project (CI-gated evals). Cost accepted: maintaining provider adapters and a growing eval suite. Initial role defaults are deliberately not recorded in this ADR; they belong to eval output, which changes without ceremony.

## References

- docs/research/middleware-survey.md, section 5
- Comparative model evaluations reviewed 2026-07-29: opper.ai model comparisons, respan.ai fast-model comparison, macaron.im cost-tier breakdown, kunalganglani.com production pipeline notes, evolink.ai Gemini/Haiku analysis
