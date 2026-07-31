# BUILD-CHECKLIST.md

The build plan for V1. Claude Code executes this top to bottom, one step at a time, checking off as it goes.

How to use this file:

1. Do steps in order. Never skip, never batch, never improvise scope.
2. 🔶 means stop and get Brandon's decision or demo approval before proceeding.
3. Every step lists `refs` (the governing documents) and `done` (the check that closes it). A step without its done-check passing is not complete.
4. Each Gate ends a phase: run it, then rewrite STATE.md to reflect the new reality, then commit.
5. If a step reveals a needed decision not covered by an ADR, stop, propose the ADR in chat, and only build after it is accepted.

References used throughout: CLAUDE.md (rules), STATE.md (V1 definition and acceptance tests), protocol/SPEC.md (the contract), protocol/schemas/protocol.schema.json (source of truth), docs/adr/ (decisions).

---

## Phase 0 · Skeleton and tooling

- [x] 0.1 🔶 Ratify ADR-0008 (Python core). Brandon flips Status to Accepted, or vetoes and this checklist's tooling steps get rewritten.
      refs: docs/adr/0008-python-core.md
      done: ADR-0008 Status is Accepted and committed.
- [x] 0.2 🔶 License decision. Repo is public; pick a license or explicitly record "no license yet" in STATE.md open decisions.
      refs: STATE.md
      done: LICENSE file committed, or STATE.md records the deliberate deferral.
- [x] 0.3 Scaffold: `uv init`, Python 3.12, `src/` layout with packages `brain/`, `bodies/`, `wire/`; `ruff` and `pytest` configured; `.env.example` with `BRAIN_` variables only.
      refs: ADR-0000 (naming containment), ADR-0008
      done: `uv run pytest` passes on an empty test; `ruff check` clean.
- [x] 0.4 Schema loader: the `src/wire/` package loads `protocol/schemas/protocol.schema.json`, defines the envelope models, and exposes a validator. Recreate the fixture suite from Batch 2 review: 18 valid messages (full handshake, commands, results, E-stop, state events, unknown-future-fields message) and 9 invalid ones (missing ttl_ms, missing span_id, bad version format, missing session, bad status, empty estop reason, malformed body_id, unknown type, missing ts).
      refs: SPEC §4-6, schema file
      done: pytest asserts 18 pass and 9 fail, wired into the suite permanently.
- [x] 0.5 CI: GitHub Actions workflow running ruff and pytest on push.
      done: first green run on GitHub.

**Gate 0:** suite green in CI. Rewrite STATE.md (phase: building, Phase 0 complete). Commit.

## Phase 1 · Session core (brain as server)

- [x] 1.1 Envelope models (pydantic) mirroring the schema; every inbound and outbound message validated at the boundary in both directions.
      refs: SPEC §4; CLAUDE.md rule 3
      done: round-trip property test: model → JSON → schema-valid → model.
- [x] 1.2 WebSocket server (`websockets` lib): subprotocol check (`body-adapter-protocol.v1`), hello/welcome/reject, constant-time auth token compare (`BRAIN_AUTH_TOKEN`), version negotiation exactly per SPEC §5.
      refs: SPEC §3, §5, §6.1-6.3
      done: integration test with a scripted client: good hello gets welcome; wrong token gets reject auth_failed; unknown version gets reject with supported list; wrong subprotocol refused at upgrade.
- [x] 1.3 Session registry: session ids, per-sender seq tracking (gap detection logged), t_received stamping on every inbound message.
      refs: SPEC §4
      done: unit tests for seq gaps and duplicate handling.
- [x] 1.4 Brain-side heartbeat loop and lease detection: send on interval, mark body LOST on lease miss, fail outstanding spans brain-side.
      refs: SPEC §6.4, §8.1; ADR-0006
      done: test with a client that goes silent; LOST within lease_ms, spans failed.

**Gate 1:** all Phase 1 integration tests green. Rewrite STATE.md. Commit.

## Phase 2 · Flight recorder

- [x] 2.1 MCAP writer: one channel per message type plus a `session_meta` channel (negotiated version, manifest); every rx/tx message logged with dual timestamps, seq, trace_id, span_id.
      refs: ADR-0005; SPEC §4
      done: recorded file opens in Foxglove or the `mcap` CLI; channel count and message counts assert in a test.
- [ ] 2.2 Replay reader: load a session file, reconstruct the ordered message sequence, re-feed programmatically.
      refs: ADR-0005
      done: record a synthetic session, replay it, assert the reconstructed sequence is byte-identical.
- [x] 2.3 Define (empty for now) the `llm_io` channel schema: prompt, response, model, role, token counts, latency. Populated in Phase 5.
      refs: ADR-0005, ADR-0007
      done: channel exists and is asserted in the writer test.

**Gate 2:** the seed of acceptance test 3 passes (synthetic session replays identically). Rewrite STATE.md. Commit.

## Phase 3 · Mock body and safety semantics

- [x] 3.1 Mock body adapter, separate process, connects as a client: manifest with `sys`, `differential_drive`, `range_sensor`; `boot_state: safe_hold`; fake odometry and range events on a timer.
      refs: SPEC §7; ADR-0003
      done: handshake completes; manifest validates; sys state event emitted on boot.
- [x] 3.2 Body-side command semantics: TTL enforcement from receipt time, span_id dedupe within session, exactly one terminal result per span.
      refs: SPEC §6.6-6.7
      done: tests for expired TTL (terminal expired, never executed), duplicate span (single execution), double-terminal prevented.
- [x] 3.3 Latching: safe_hold on brain-heartbeat lease miss; estop and estop_clear per spec; clear_safe_hold via sys; no self-clearing on reconnect.
      refs: SPEC §8.1-8.4; ADR-0006
      done: tests: silence the brain, body latches within lease; reconnect keeps the latch; estop latches even while idle; estop_clear plus clear_safe_hold restore ok.
- [ ] 3.4 Brain-side validator v1: schema check, manifest grounding (capability id, action, class-registry membership), bounds from attributes (max velocities), per-body rate limit, every rejection logged to MCAP with reason.
      refs: ADR-0004; SPEC §7.1; ADR-0005
      done: table-driven tests: out-of-manifest action rejected, over-bounds velocity clamped or rejected per config, rejection visible in the log.

**Gate 3 = acceptance test 1:** scripted run: mission command in flight, kill the socket, assert the mock body reaches safe_hold within its TTL and latches; estop drill passes. 🔶 Demo this to Brandon before continuing.

## Phase 4 · Laptop body

- [x] 4.1 Camera capability: `snapshot` via OpenCV/AVFoundation, jpeg base64 `frame` events per SPEC §6.5, modest resolution defaults.
      refs: SPEC §6.5, §7.2
      done: snapshot round trip lands in MCAP and decodes to a valid image in a test.
- [x] 4.2 Microphone capability: push-to-talk capture (`sounddevice`), `audio_chunk` events with declared sample rate and encoding.
      done: captured clip round-trips and plays back.
- [ ] 4.3 Speaker capability: `say` via macOS `say` for now (manifest declares `tts: local`); Kokoro replaces it in Phase 6.
      done: say command produces audio and a terminal succeeded result.
- [ ] 4.4 The Viam test: connect the laptop body against a brain untouched since Gate 3. Record the brain-side git diff since Gate 3; it must contain no changes outside `bodies/`.
      refs: ADR-0001, ADR-0003; STATE.md capability 3
      done: assertion documented in the PR description with the diff.

**Gate 4:** 🔶 live demo: snapshot on request, spoken sentence on request. Rewrite STATE.md. Commit.

## Phase 5 · LLM layer, planner, memory

- [ ] 5.1 Provider-agnostic LLM interface with adapters: Anthropic, OpenAI, Google, Ollama. Routing table in config keyed by role: planner, conversation, vision, classifier, reflex. Token counts and latency to the `llm_io` channel on every call.
      refs: ADR-0007, ADR-0005
      done: same prompt runs through all four adapters in a smoke test (skippable per-provider by env).
- [ ] 5.2 Eval harness v0: about 20 cases across plan decomposition, capability grounding, structured-output validity, and refusal of infeasible commands; scoring script outputs a comparison table.
      refs: ADR-0007
      done: harness runs against at least three models and produces the table. 🔶 Brandon reviews results and sets the initial routing table.
- [ ] 5.3 Planner (slow loop): goal → structured BT-like plan (JSON, its own schema in `protocol/schemas/plan.schema.json`), grounded to the connected manifest, event-triggered only, every proposal through the validator.
      refs: ADR-0004; ADR-0003; SPEC §7.1
      done: given a goal and the mock manifest, an executable valid plan emerges; an infeasible goal ("fly") yields a refusal, not a plan.
- [ ] 5.4 Fast-loop executor: runs plan steps against bodies, reflex hooks, fully functional with the LLM offline; brain FSM (BOOT, IDLE, ACTIVE, DEGRADED, E-STOP) drives heartbeat state.
      refs: ADR-0004; SPEC §6.4, §8.5
      done: acceptance test 2 passes: LLM adapter forcibly erroring, system stays responsive, DEGRADED visible in heartbeats, safety tests still green.
- [ ] 5.5 Memory v0: structured world and task state persisted to disk, survives restart; curator pass rewrites (never appends) bounded context for the planner.
      refs: STATE.md capability 6; survey §5
      done: restart mid-task, planner context reconstructed from persisted state in a test.

**Gate 5:** 🔶 first end-to-end mission on the mock body, demoed: plain-language goal, plan, validated execution, report, all visible in the MCAP log. Rewrite STATE.md. Commit.

## Phase 6 · Voice

- [ ] 6.1 Local STT: whisper.cpp or mlx-whisper behind a small interface; push-to-talk clip → transcript.
      refs: survey §6
      done: spoken sentence transcribes within acceptable latency on the M4.
- [ ] 6.2 Kokoro TTS replaces `say`; sentence-streamed playback.
      done: speaker capability now declares and uses Kokoro; audible quality check. 🔶
- [ ] 6.3 Conversation loop: push-to-talk → STT → planner/conversation role with perception grounding → TTS. "What do you see?" answered from a fresh webcam snapshot, and remembered in task state.
      refs: STATE.md capability 1
      done: the exchange works end to end and appears fully in the MCAP log.

**Gate 6 = the V1 demo scene** from STATE.md, run live: converse, mission on mock body, mid-mission socket kill fails safe, replay the session afterward. 🔶 Brandon runs it personally.

## Phase 7 · Hardening and acceptance

- [ ] 7.1 Acceptance test 4 drill: make a backward-compatible schema change (new optional field), regenerate nothing body-side, assert the un-updated mock body still fully works.
      refs: SPEC §9; ADR-0002
      done: drill documented; old body green.
- [ ] 7.2 Full deterministic replay pass over a real (not synthetic) session including LLM decisions.
      refs: ADR-0005
      done: acceptance test 3 green on a genuine session.
- [ ] 7.3 README for the public repo (working-title notice, what this is, architecture sketch, how to run the demo); STATE.md rewritten to V1-complete; ADR sweep for any drift between records and reality. Revisit schema packaging as package data: v1 finds `protocol/schemas/` by walking up from the installed module, with `BRAIN_PROTOCOL_SCHEMA` as the override, which works from a checkout but not from a wheel installed away from the repo tree. Proper packaging fixes this and the `PYTHONPATH=src` requirement together: both exist only because the project is run from a source tree rather than installed. Root cause of the second: uv writes its editable-install `.pth` with the macOS `UF_HIDDEN` flag, and CPython's `site.addpackage` silently skips hidden `.pth` files.
      done: fresh-instance test: a new Claude Code session, given only the repo, correctly explains the system and runs the test suite.

**Done:** all four acceptance tests in STATE.md green. V1 exists.
