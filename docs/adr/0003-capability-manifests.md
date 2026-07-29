# ADR-0003: Capability manifests with a do_command escape hatch

- Status: Accepted
- Date: 2026-07-29

## Context

The architecture's central promise is that bodies swap without brain changes. That requires the brain to learn what a body can do from the body itself, in machine-readable form, rather than having body knowledge compiled in. Prior art: Viam's standardized per-capability APIs with a generic DoCommand on every resource, and Home Assistant's split between device integrations (connection) and entity classes with device_class semantics (capability), which scaled to over a thousand integrations.

## Decision

Every body declares a capability manifest at handshake, and the brain interacts through standard capability APIs plus a generic escape hatch.

1. The manifest contains: body id, hardware class, protocol version, and a list of capabilities, each typed by a standard capability class (`camera`, `microphone`, `speaker`, `differential_drive`, `range_sensor`, ...) with attributes (resolution, rate, units).
2. Each capability class has a small standardized API. Every capability additionally exposes `do_command` for functionality the standard API does not cover, so nonstandard features never fork the protocol.
3. The brain discovers via an explicit `list_capabilities` request. Discovery is pull-based, never broadcast.
4. The manifest is the planner's affordance function: the planner may only propose actions present in the connected body's manifest (the SayCan grounding pattern). The validator enforces this.

## Consequences

Bodies swap freely; the Viam test ("new body, zero brain changes") becomes checkable. The manifest does double duty as safety grounding, which couples ADR-0003 to ADR-0004 and ADR-0006 deliberately. Cost: capability class APIs must be designed carefully, since they are the protocol's public surface (Batch 2 spec).

## References

- docs/research/middleware-survey.md, sections 1 (Viam, Home Assistant) and 2
