# ADR-0006: Safety scaffolding exists before any motor does

- Status: Accepted
- Date: 2026-07-29

## Context

Retrofitting safety after actuators arrive is the classic compounding mistake in robot software. Every mechanism below is fully exercisable with the laptop body and a mock body, which makes now the cheapest possible time to build and continuously test it. Patterns are drawn from flight-controller failsafe practice (tiered timeouts, latching modes) and ROS 2 QoS semantics (lifespan).

## Decision

The following exist from the first commit and gate all body interaction:

1. **Command validator.** Already decided in ADR-0004; restated here as a safety control: no command reaches a body without passing schema, capability, bounds, and rate checks. Rejections are logged, never silent.
2. **Tiered heartbeats with latching safe-state.** Control-link heartbeat on the order of 1 second, supervisory on the order of 5 seconds (flight-controller pattern). On loss, the affected body enters a defined safe state and stays there until explicitly cleared. No silent resume.
3. **Command TTL.** Every actuation command carries a lifespan; a body that receives nothing fresh within the TTL stops. Stale commands cannot keep anything moving, which makes network loss inherently safe.
4. **Global E-stop.** A dedicated highest-priority stop path bypassing normal flow. Model: stopped is the default state, and motion requires continuously refreshed permission.
5. **DEGRADED mode.** LLM or network loss drops the brain to reflex-only conservative behavior; total brain loss leaves each body in body-local safe hold.

## Consequences

Failure of any link, process, or model degrades toward stillness, never toward uncontrolled motion, and this property is tested weekly rather than discovered in an incident. Explicit limit: this is software scaffolding, not certified functional safety. When actuators capable of injury arrive, hardware-level safety (ISO 13849-class relays, dual-channel E-stop) is required in addition, and a new ADR must record that design.

## References

- docs/research/middleware-survey.md, section 4
