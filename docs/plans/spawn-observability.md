## Why

Spawn trees are the product, but the surfaces are still flat: no hierarchy rendering, no cancellation origin, no test-injection facility for lifecycle states, and Pi's /spawn row inspection reuses a generic overlay.

## Goal

A user (or agent) can see what a spawn tree did and why it stopped, from any surface.

## Summary

Planning draft. Scope, per issue:

- **Closes #135** — document the `--bg` lifecycle and surface cancellation origin in spawn detail output.
- **Closes #130** — render parent-child hierarchy in the work dashboard and spawn list (parent_id already persisted).
- **Closes #27** — spawn-event test injection facility so lifecycle states can be constructed in tests without real processes.
- **Closes #285** — dedicated Pi /spawn row detail overlay instead of the generic log overlay.

## Resulting Behavior

`meridian work` and `meridian spawn list` show trees, terminal states carry their origin, and lifecycle tests stop reverse-engineering state files.

## Changes

Depends loosely on the state-store-scaling workstream's #129 (canonical events.jsonl) — cancellation-origin surfacing gets much easier after it; sequence this PR after or alongside.

## Work Item

issue-triage-sweep

## Verification

None yet — this is a planning draft (scoping commit only: `docs/plans/spawn-observability.md`). Implementation on this branch will carry its own tests per `tests/AGENTS.md` and a CHANGELOG entry.

## Knowledge Updates

Plan doc committed at `docs/plans/spawn-observability.md`. Triage evidence lives in the docs repo under `work/issue-triage-sweep/`.

## Spawn Trace

- p5268, p5269 (terra explorers) — issue triage lanes C/D
- two native sonnet reviewer lanes — issue triage lanes A/B
