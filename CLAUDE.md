# CLAUDE.md

Working title: **ai-brain**. This is a placeholder, not the name. Rules in `docs/adr/0000-project-name-deferred.md`.

## What this project is

A single shared robot brain: LLM planning, perception, memory, safety, and telemetry in one core, controlling many simple bodies through a versioned adapter protocol. Operating system plus drivers: bodies are cheap and swappable, the brain compounds. Physical-first roadmap, embodiment-agnostic architecture.

## Orientation order for a fresh instance

1. This file.
2. `STATE.md` · what exists right now, current phase, what is next. Under a thousand tokens, always current.
3. `protocol/SPEC.md` · the brain-to-body contract, if the task touches messaging, capabilities, or safety.
4. `docs/adr/` · read the specific ADR whenever you are about to question or change a decision. The index is the filenames.
5. `docs/research/middleware-survey.md` · deep background and sources. Read sections on demand, not up front.

## Rules of the repo

1. **Decisions are ADRs.** Significant choices get a record in `docs/adr/` using `template.md`. Accepted ADRs are frozen; changing course means a new ADR with a Supersedes line. Never edit an accepted ADR beyond flipping its Status to superseded.
2. **STATE.md is a snapshot, not a log.** Rewrite it whole when reality changes. Never append history to it; history lives in ADRs and git.
3. **The schema is the source of truth.** `protocol/schemas/protocol.schema.json` governs the wire format. Code validates against it at runtime and in tests. Prose (including SPEC.md) that disagrees with the schema is a bug in the prose. Never duplicate schema content into docs.
4. **Placeholder containment (ADR-0000).** No brand strings in code. Domain terms only: `brain`, `adapter`, `body`, `planner`, `validator`, `reflex`. Env vars use the `BRAIN_` prefix. The protocol identifies itself as `body-adapter-protocol.v1`.
5. **Safety scaffolding is not optional or deferrable (ADR-0006).** Validator, heartbeats, TTL, E-stop, latching. Any change touching command flow keeps every acceptance test in STATE.md green.
6. **The LLM never holds actuator authority (ADR-0004).** Plans are proposals; the deterministic validator disposes. Do not put LLM calls on the hot path.
7. **Log the why (ADR-0005).** New event flows must land in MCAP with trace and span ids, timestamps per spec section 4, and full LLM I/O capture.

## How we work

- Planning and decisions happen in chat with Brandon. Building happens here through `BUILD-CHECKLIST.md`, one step at a time, in order. Do not skip ahead, do not improvise scope.
- 🔶 marks a step needing Brandon's input or approval. Stop and ask; never guess past a 🔶.
- Nothing is real until committed. If a decision was made in chat but is not in an ADR, propose the ADR before building on it.
- Verify against acceptance tests in STATE.md after any change to core behavior, and run the protocol fixture suite after any change near the wire format.

## Style

- Markdown prose in this repo never uses em dashes. Use periods, commas, colons, or an interpunct (·). This is an intentional authoring rule, not an accident.
- Keep documents short and current over long and stale. If a doc is growing a history section, it is trying to become an ADR or a git log; split it.
