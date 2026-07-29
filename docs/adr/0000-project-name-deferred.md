# ADR-0000: Project name deferred

- Status: Accepted
- Date: 2026-07-29

## Context

A naming session was held before the first commit and did not converge on a name worth keeping. The project must start now, and the eventual product/company name should not be blocked on branding. The risk of a placeholder is that it quietly ossifies into code, protocol strings, and package names, making the real rename expensive.

## Decision

The working title is **ai-brain**, and it is explicitly a placeholder. The placeholder lives in documentation only, never in code.

Rules:

1. The repo is named `ai-brain`. README and CLAUDE.md open with a working-title notice.
2. All code identifiers use domain terms that remain correct under any brand: `brain`, `adapter`, `body`, `planner`, `validator`, `reflex`.
3. Environment variables use the `BRAIN_` prefix.
4. The protocol identifies itself generically in the handshake (e.g. `body-adapter-protocol/v1`), never by project name.
5. No brand string in package names, log prefixes, schema names, database names, or bundle identifiers.

## Consequences

The eventual rename touches the repo name and a few doc headers · roughly an hour of work. Nothing inside the codebase changes, because nothing inside the codebase was ever branded. When the real name is chosen, a new ADR records it and supersedes this one.
