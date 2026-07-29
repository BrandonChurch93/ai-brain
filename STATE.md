# STATE.md

Last rewritten: 2026-07-29. Rewrite this file whole when reality changes; never append history to it.

Working title **ai-brain** (placeholder, ADR-0000).

## Phase

Pre-build. Foundation documents complete and committed: ADRs 0000 through 0008 (eight accepted plus 0008 proposed, ratification is checklist step 0.1), research survey, protocol SPEC and schema (validated against 27 message fixtures). Build has not started; no code exists yet.

## What exists

- `docs/adr/` · ADRs 0000 through 0008 plus template. Eight accepted; 0008 is proposed, ratification is checklist step 0.1.
- `docs/research/middleware-survey.md` · prior-art survey, the ADR citation base
- `protocol/SPEC.md` and `protocol/schemas/protocol.schema.json` · the brain-to-body contract, v1, version string `2026-07-29`
- This file and `CLAUDE.md`

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

Non-goals for V1: purchased hardware, navigation or SLAM, learned policies or VLA, wake word or full-duplex voice, fleet features, MQTT or Zenoh, any dashboard beyond basic telemetry. Each has a designed seam; none belongs in the foundation.

## Open decisions

- 🔶 Ratify ADR-0008 (Python core for V1). Checklist step 0.1.
- 🔶 Repository license. Checklist step 0.2.

## Next

1. Resolve the two open decisions above.
2. Begin Phase 0 of `BUILD-CHECKLIST.md`.
