# ADR-0005: MCAP logging and deterministic replay from day one

- Status: Accepted
- Date: 2026-07-29

## Context

Robot bugs are timing bugs, and an LLM-in-the-loop system adds nondeterministic decisions on top. Without a full-fidelity record, "why did it do that?" is unanswerable. The robotics field converged on MCAP (chunked, indexed, self-describing, now the ROS 2 default bag format, supported by Foxglove and related tooling). Practitioner guidance: record the low-rate supervisory streams that explain behavior, not just sensor data. Replay also has a direct cost benefit: recorded LLM outputs are re-fed, not re-purchased.

## Decision

MCAP is the log format from the first commit, and replay is a first-class mode.

1. Everything is logged: perception events, plans, full LLM requests and responses, validator verdicts including rejections, commands, heartbeats, safety events, and token counts per call.
2. Every event carries dual timestamps (monotonic for ordering, UTC for correlation), `t_captured` and `t_received`, and a per-stream sequence number.
3. Every goal gets a `trace_id`; every command gets a `span_id` threaded from plan through execution to feedback.
4. Replay mode re-feeds recorded nondeterministic inputs (LLM outputs, sensor frames, RNG seeds) so a session reproduces identical decisions.

## Consequences

The brain has a flight recorder: post-hoc debugging, free regression tests, and zero-cost re-testing of past sessions. Disk is cheap; the discipline of logging the "why" streams is the real cost. One V1 acceptance test depends directly on this ADR: replaying a full session yields identical decisions.

## References

- docs/research/middleware-survey.md, section 3
