# STATE.md

Last rewritten: 2026-07-30. Rewrite this file whole when reality changes; never append history to it.

Working title **ai-brain** (placeholder, ADR-0000).

## Phase

Building. Phase 0 of `BUILD-CHECKLIST.md` is complete: the project is scaffolded, the wire package loads and enforces the protocol schema, and CI is green on GitHub. Nothing talks to anything yet: there is no server, no body, no session. Phase 1 (session core) is next.

## What exists

Documents:

- `docs/adr/` · ADRs 0000 through 0008 plus template. All nine accepted.
- `docs/research/middleware-survey.md` · prior-art survey, the ADR citation base
- `protocol/SPEC.md` and `protocol/schemas/protocol.schema.json` · the brain-to-body contract, v1, version string `2026-07-29`, schema id `urn:body-adapter-protocol:2026-07-29`
- This file, `CLAUDE.md`, `BUILD-CHECKLIST.md`, and a minimal `README.md`

Code:

- Python 3.12 managed by `uv`, `ruff` and `pytest` configured, `src/` layout with `brain/`, `bodies/`, `wire/`. Distribution name is `brain`; no placeholder string appears anywhere in code (ADR-0000).
- `src/wire/` · loads the schema and validates messages against it. Message types and capability classes are read out of the schema rather than restated, so they cannot drift. Validation errors are flattened to leaves, because the envelope dispatches on `type` through if/then subschemas and the unflattened error names no cause.
- `tests/` · 39 tests. The permanent protocol fixture suite is 27 wire messages on disk: 18 the schema must accept (all 12 message types, both body shapes, the reserved `sys` state event, and a message from a hypothetical later schema carrying unknown fields) and 9 it must reject, each asserting the specific schema rule it exists to defend.
- `.github/workflows/ci.yml` · ruff, format check, and pytest on every push. Green.

## V1 definition

V1 is the brain proven, not the brain applied. It is done when the demo scene below passes.

Capabilities:

1. Push-to-talk voice conversation grounded in perception: local STT (Whisper) and TTS (Kokoro), the person asks what the brain sees and it answers from the webcam and remembers.
2. End-to-end missions: a plain-language goal is decomposed by the planner LLM into a validator-checked plan grounded against the connected body's capability manifest, executed by the fast loop, results reported back.
3. Two bodies, zero brain changes: the laptop body (camera, microphone, speaker) and a pure mock body (fake drive, fake range sensor) both connect through the versioned handshake and work.
4. Safe failure, always: heartbeat leases with latching safe-state, command TTLs, global E-stop, DEGRADED mode with the reflex tier keeping things responsive when the LLM or network drops.
5. Full flight recorder: every perception, plan, LLM exchange, validator verdict, and command in MCAP with trace and span ids; any session replayable deterministically.
6. Persistent memory: structured world and task state that survives restarts, with a curator keeping context bounded.
7. Deployment portability: config-driven, clean environment, nothing Mac-specific in the core.

Acceptance tests (the definition of done):

- Kill the WebSocket mid-mission; the mock body reaches safe-hold within its TTL and latches.
- Take the LLM offline; the system stays responsive and safe in DEGRADED mode.
- Replay yesterday's session from the MCAP log; decisions come out identical.
- Make a backward-compatible protocol change; the un-updated body still works.

None are green yet. Each needs machinery that does not exist: acceptance tests 1 and 2 need Phase 3 and Phase 5, test 3 needs Phase 2, test 4 needs Phase 7.

Non-goals for V1: purchased hardware, navigation or SLAM, learned policies or VLA, wake word or full-duplex voice, fleet features, MQTT or Zenoh, any dashboard beyond basic telemetry. Each has a designed seam; none belongs in the foundation.

## Open decisions

- License (deliberately deferred at step 0.2; decide by V1 complete, expected shape is an open protocol with core licensing decided separately, will get its own ADR).

## Next

Phase 1 of `BUILD-CHECKLIST.md`, the session core: pydantic envelope models, the WebSocket server with handshake and version negotiation, the session registry, and the brain-side heartbeat lease.
