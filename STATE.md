# STATE.md

Last rewritten: 2026-07-30. Rewrite this file whole when reality changes; never append history to it.

Working title **ai-brain** (placeholder, ADR-0000).

## Phase

Building. Phases 0 through 2 are complete: the brain is a WebSocket server that accepts bodies, negotiates a protocol version, tracks sessions, fails safe when a body goes quiet, and records every exchange to an MCAP file that replays byte-identically. No body exists yet to connect to it. Phase 3 (mock body and safety semantics) is next.

## What exists

Documents:

- `docs/adr/` · ADRs 0000 through 0008 plus template. All nine accepted.
- `docs/research/middleware-survey.md` · prior-art survey, the ADR citation base
- `protocol/SPEC.md` and `protocol/schemas/protocol.schema.json` · the brain-to-body contract, v1, version string `2026-07-29`, schema id `urn:body-adapter-protocol:2026-07-29`
- This file, `CLAUDE.md`, `BUILD-CHECKLIST.md`, and a minimal `README.md`

Code (227 tests, green in CI on every push):

- Python 3.12 managed by `uv`, `ruff` and `pytest` configured, `src/` layout with `brain/`, `bodies/`, `wire/`. Distribution name is `brain`; no placeholder string appears anywhere in code (ADR-0000).
- `src/wire/` · the schema is loaded, never restated. Message types, capability classes, and the negotiated protocol version are all read out of it, so they cannot drift from it. Pydantic models own structure, the schema owns constraints, and the codec validates in both directions, so the brain cannot emit a message it would itself refuse. Unknown fields survive a round trip intact.
- `src/brain/` · WebSocket server (subprotocol enforced at the upgrade, constant-time auth, version negotiation), session registry (per-sender `seq` with gap and duplicate detection, brain-side `t_received` stamped before parsing), span tracking, and the heartbeat loop with lease detection.
- `src/brain/recorder.py` and `replay.py` · the flight recorder. Fourteen MCAP channels: one per message type, plus `session_meta` and `llm_io` (defined, populated in Phase 5). The protocol schema is embedded in every file, so a session is self-describing. Replay reconstructs a session byte-identically, ordered by a file-wide index rather than by timestamps.
- `tests/` · the permanent 27-message protocol fixture suite, a schema-derived round-trip property test, integration tests driving the server over real sockets, and MCAP conformance run through the independent `mcap` CLI.
- `.github/workflows/ci.yml` · ruff, format check, pytest, and the `mcap` CLI conformance check on every push.

## What does not exist yet

No body of any kind, so nothing has ever connected to the server except test clients. No LLM, planner, or validator, so `llm_io` is an empty channel. No safety behaviour on the body side, which is where latching actually lives. No persistence beyond the session logs: no world or task state survives a restart.

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

None are green yet. Test 1 needs Phase 3, test 2 needs Phase 5, test 4 needs Phase 7.

Test 3 has its seed: a synthetic session replays byte-identically. It is not green, because a genuine session means a real body and recorded LLM decisions, and neither exists. Step 7.2 is where it counts.

The brain half of test 1 is built: a silent body is marked LOST within its lease and its outstanding spans fail. The body half, which is the latching that makes it a safety property rather than bookkeeping, is Phase 3.

Non-goals for V1: purchased hardware, navigation or SLAM, learned policies or VLA, wake word or full-duplex voice, fleet features, MQTT or Zenoh, any dashboard beyond basic telemetry. Each has a designed seam; none belongs in the foundation.

## Open decisions

- License (deliberately deferred at step 0.2; decide by V1 complete, expected shape is an open protocol with core licensing decided separately, will get its own ADR).

## Next

Phase 3 of `BUILD-CHECKLIST.md`, the mock body and safety semantics: a body adapter in its own process, command TTL and span deduplication, latching `safe_hold` and E-stop, and the brain-side validator. Gate 3 is V1 acceptance test 1 and needs a demo.
