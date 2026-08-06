# STATE.md

Last rewritten: 2026-07-31. Rewrite this file whole when reality changes; never append history to it.

Working title **ai-brain** (placeholder, ADR-0000).

## Phase

Building. Phases 0 through 4 are complete. The brain accepts bodies, negotiates a protocol version, tracks sessions, and records every exchange to an MCAP file that replays byte-identically. Two bodies exist: a mock that enforces command TTL, span deduplication and latching safety state, and a laptop body that sees through a real camera and speaks out loud. V1 acceptance test 1 passes as a live drill, and Gate 4 passed live. Phase 5 (LLM layer, planner, memory) is next, and nothing in the system decides anything yet.

## What exists

Documents:

- `docs/adr/` · ADRs 0000 through 0008 plus template. All nine accepted.
- `docs/research/middleware-survey.md` · prior-art survey, the ADR citation base
- `protocol/SPEC.md` and `protocol/schemas/protocol.schema.json` · the brain-to-body contract, v1, version string `2026-07-29`, schema id `urn:body-adapter-protocol:2026-07-29`
- This file, `CLAUDE.md`, `BUILD-CHECKLIST.md`, and a minimal `README.md`

Code (383 tests, green in CI on every push):

- Python 3.12 managed by `uv`, `ruff` and `pytest` configured, `src/` layout with `brain/`, `bodies/`, `wire/`. Distribution name is `brain`; no placeholder string appears anywhere in code (ADR-0000).
- `src/wire/` · the schema is loaded, never restated. Message types, capability classes, and the negotiated protocol version are all read out of it, so they cannot drift from it. Pydantic models own structure, the schema owns constraints, and the codec validates in both directions, so the brain cannot emit a message it would itself refuse. Unknown fields survive a round trip intact.
- `src/brain/` · WebSocket server (subprotocol enforced at the upgrade, constant-time auth, version negotiation), session registry (per-sender `seq` with gap and duplicate detection, brain-side `t_received` stamped before parsing), span tracking, and the heartbeat loop with lease detection.
- `src/brain/recorder.py` and `replay.py` · the flight recorder. Fourteen MCAP channels: one per message type, plus `session_meta` and `llm_io` (defined, populated in Phase 5). The protocol schema is embedded in every file, so a session is self-describing. Replay reconstructs a session byte-identically, ordered by a file-wide index rather than by timestamps.
- `src/bodies/` · the body half of the protocol, and two bodies built on it. Command TTL measured from receipt, span deduplication, exactly one terminal result per span, deferred spans for actions that outlive their own TTL, and latched safety state that survives reconnection.
  - The mock body: fake drive and range sensor, the reference implementation of the SPEC section 10 conformance checklist. Runs as `mock-body`.
  - The laptop body: real camera and real speech, plus a microphone. Devices are probed at startup and the manifest declares what they actually opened at, never what was requested; a device that will not open is left out rather than promised. Runs as `laptop-body`. Capture dependencies are an optional extra, so the default environment and CI need no hardware.
- `tests/` · the permanent 27-message protocol fixture suite, a schema-derived round-trip property test, integration tests over real sockets, MCAP conformance through the independent `mcap` CLI, and the adapter conformance suite covering all ten mechanically checkable lines of SPEC section 10 for every body.
- `scripts/drill`, `scripts/gate4`, `scripts/viam_test` · the three live demonstrations: the socket-kill safety drill, the see-and-speak gate, and the laptop body proven against a brain from before it existed.
- `.github/workflows/ci.yml` · ruff, format check, pytest, and the `mcap` CLI conformance check on every push.

## What does not exist yet

No LLM, planner, or validator. `llm_io` is a defined and empty channel, and nothing in the system decides anything: every command so far was sent by a script. No persistence beyond the session logs, so no world or task state survives a restart. The microphone works and is unused, because nothing yet listens; it earns its place with the voice loop in Phase 6.

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

Test 1 is green. `scripts/drill` runs it live: a mission underway, the socket killed, the body latching `safe_hold` within its lease with no working network, its in-flight actuation span failed, the latch surviving reconnection, and the two-step E-stop recovery. Brandon ran it at Gate 3.

Tests 2 and 4 are not green: test 2 needs Phase 5, test 4 needs Phase 7.

Test 3 has its seed: a synthetic session replays byte-identically. It is not green, because a genuine session means recorded LLM decisions and those do not exist yet. Step 7.2 is where it counts.

Capability progress against the V1 list: capability 3 is substantially proven, since both bodies connect through the same handshake and step 4.4 showed the laptop body working against a brain that predates it, with `git log` over `src/brain/` empty across the whole of Phase 4. Capability 1 is part built: the laptop can see and speak, and the perception, STT, TTS and memory that would make it a conversation are Phases 5 and 6. Capability 4 is proven on the wire and awaits the DEGRADED half in Phase 5.

Non-goals for V1: purchased hardware, navigation or SLAM, learned policies or VLA, wake word or full-duplex voice, fleet features, MQTT or Zenoh, any dashboard beyond basic telemetry. Each has a designed seam; none belongs in the foundation.

## Open decisions

- License (deliberately deferred at step 0.2; decide by V1 complete, expected shape is an open protocol with core licensing decided separately, will get its own ADR).

## Next

Phase 5 of `BUILD-CHECKLIST.md`: the provider-agnostic LLM layer, an eval harness to decide the routing table, the planner, the fast-loop executor with the brain FSM, and memory that survives a restart. This is where the system starts deciding things rather than being told them.
