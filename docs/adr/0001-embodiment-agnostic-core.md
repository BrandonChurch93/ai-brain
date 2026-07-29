# ADR-0001: Embodiment-agnostic core, physical-first roadmap

- Status: Accepted
- Date: 2026-07-29

## Context

The brain is a single shared orchestration core (LLM planning, perception, memory, safety, telemetry) intended to serve many robot bodies over time. The same architecture could also serve non-physical problems: a satellite feed, hydrophone stream, or software integration is structurally a sensor body without motors. The virtual-agent market is crowded (existing agent frameworks, coding agents, MCP ecosystem); the physical side carries the project's mission, differentiation, and the author's stated excitement.

## Decision

The core is embodiment-agnostic by architecture and physical-first by roadmap.

1. A "body" is any adapter that connects and declares capabilities. Nothing in the core assumes capabilities are physical.
2. No feature is added to the core that only makes sense for virtual agents. Virtual capability arrives later as just another adapter, when a real problem demands it.
3. Per-problem work (adapters, skills, perception models) lives in problem layers above the core, never inside it.

## Consequences

Dual-domain reach is preserved as an inherited property at zero cost, not chased as a scope. The standing test of the boundary: solving a new problem, physical or virtual, requires new adapters and skills only, with zero changes to brain code. If a new problem forces a core change, the abstraction was drawn wrong and this ADR should be revisited.

## References

- docs/research/middleware-survey.md, section 7 (synthesis)
