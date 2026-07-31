# STATE.md

Last rewritten: 2026-07-31. Rewrite this file whole when reality changes; never append history to it.

Working title **ai-brain** (placeholder, ADR-0000).

## Phase

Building. Phases 0 through 3 are complete. The brain accepts bodies, negotiates a protocol version, tracks sessions, and records every exchange to an MCAP file that replays byte-identically. A mock body runs as its own process, enforces command TTL and span deduplication, and latches `safe_hold` and `estopped` without ever clearing itself. V1 acceptance test 1 passes as a live drill. Phase 4 (laptop body) is next.

## What exists

Documents:

- `docs/adr/` · ADRs 0000 through 0008 plus template. All nine accepted.
- `docs/research/middleware-survey.md` · prior-art survey, the ADR citation base
- `protocol/SPEC.md` and `protocol/schemas/protocol.schema.json` · the brain-to-body contract, v1, version string `2026-07-29`, schema id `urn:body-adapter-protocol:2026-07-29`
- This file, `CLAUDE.md`, `BUILD-CHECKLIST.md`, and a minimal `README.md`

Code (314 tests, green in CI on every push):

- Python 3.12 managed by `uv`, `ruff` and `pytest` configured, `src/` layout with `brain/`, `bodies/`, `wire/`. Distribution name is `brain`; no placeholder string appears anywhere in code (ADR-0000).
- `src/wire/` · the schema is loaded, never restated. Message types, capability classes, and the negotiated protocol version are all read out of it, so they cannot drift from it. Pydantic models own structure, the schema owns constraints, and the codec validates in both directions, so the brain cannot emit a message it would itself refuse. Unknown fields survive a round trip intact.
- `src/brain/` · WebSocket server (subprotocol enforced at the upgrade, constant-time auth, version negotiation), session registry (per-sender `seq` with gap and duplicate detection, brain-side `t_received` stamped before parsing), span tracking, and the heartbeat loop with lease detection.
- `src/brain/recorder.py` and `replay.py` · the flight recorder. Fourteen MCAP channels: one per message type, plus `session_meta` and `llm_io` (defined, populated in Phase 5). The protocol schema is embedded in every file, so a session is self-describing. Replay reconstructs a session byte-identically, ordered by a file-wide index rather than by timestamps.
- `src/bodies/` · the body half of the protocol, and the mock body built on it. Command TTL measured from receipt, span deduplication, exactly one terminal result per span, and latched safety state that outlives any session. Runs as its own process (`mock-body`).
- `tests/` · the permanent 27-message protocol fixture suite, a schema-derived round-trip property test, integration tests over real sockets, MCAP conformance through the independent `mcap` CLI, and the adapter conformance suite covering all ten mechanically checkable lines of SPEC section 10 for every body.
- `scripts/drill` · V1 acceptance test 1, run as a live demonstration.
- `.github/workflows/ci.yml` · ruff, format check, pytest, and the `mcap` CLI conformance check on every push.

## What does not exist yet

No real hardware: the only body is the mock, so nothing has yet been seen, heard, or spoken. No LLM, planner, or validator, so `llm_io` is an empty channel and nothing decides anything. No persistence beyond the session logs: no world or task state survives a restart.

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

Non-goals for V1: purchased hardware, navigation or SLAM, learned policies or VLA, wake word or full-duplex voice, fleet features, MQTT or Zenoh, any dashboard beyond basic telemetry. Each has a designed seam; none belongs in the foundation.

## Open decisions

- License (deliberately deferred at step 0.2; decide by V1 complete, expected shape is an open protocol with core licensing decided separately, will get its own ADR).

## Next

Phase 4 of `BUILD-CHECKLIST.md`, the laptop body: camera, microphone, and speaker capabilities, then the Viam test, which asserts that connecting a second body required no change outside `bodies/`. The adapter conformance suite gets its second subject.
