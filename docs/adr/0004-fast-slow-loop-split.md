# ADR-0004: Fast/slow loop split; the LLM proposes, a validator disposes

- Status: Accepted
- Date: 2026-07-29

## Context

Frontier LLM planning takes seconds and can stall or go offline; reactive control and safety need tens of milliseconds and must never wait. This split is the consensus architecture in LLM robotics (SayCan's grounded planning through Figure's Helix dual-system design, which runs planning and reactive control as separate processes). A cloud LLM must also never hold direct actuator authority.

## Decision

The brain runs three decoupled tiers, as separate processes:

1. **Slow loop.** A frontier LLM, event-triggered (new goal, plan failure, significant state change, human utterance), never called per-tick. It emits a structured, behavior-tree-like plan and answers "what should be done."
2. **Fast loop.** Local and deterministic: reflex behaviors, plan execution, safety checks, heartbeat handling, running local models (Ollama/MLX) where inference is needed. It answers "how, right now," and stays fully functional when the slow loop is unavailable.
3. **Body drivers** behind the adapter protocol.

Every command from any tier passes a deterministic validator (schema, capability-manifest check, bounds, rate limits) before reaching a body. The brain's own lifecycle is a fixed FSM: BOOT, IDLE, ACTIVE, DEGRADED, E-STOP.

## Consequences

LLM latency or outage degrades intelligence, never safety or responsiveness. Tiers iterate independently. Cost accepted: two model runtimes (cloud + local) and the discipline of keeping the LLM off the hot path permanently. Plan structure and validator rules are specified in Batch 2.

## References

- docs/research/middleware-survey.md, sections 5 and 7
