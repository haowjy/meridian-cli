## Why

**Priority: low.** Command hooks shipped (#60 closed as implemented), leaving the ergonomics and extension tail: no public builtin-hook registration, config errors without line numbers, no trace command, and no generic inactivity/lifecycle event.

## Goal

The hooks system is debuggable and extensible by third parties.

## Summary

Planning draft. Scope, per issue:

- **Closes #53** — line numbers in hook config errors.
- **Closes #52** — `hooks trace` command over a persisted execution history.
- **Closes #59** — public builtin-hook protocol + registration API (currently internal).
- **Closes #51** — decide/implement a generic `session.idle`-style inactivity hook event (OpenCode emits one natively; no generic detector exists).

## Resulting Behavior

A misbehaving hook can be located (line number), replayed (trace), and third-party hooks can register without forking.

## Changes

#53/#52 are self-contained; #59/#51 need small API-surface decisions first.

## Work Item

issue-triage-sweep

## Verification

None yet — this is a planning draft (scoping commit only: `docs/plans/hooks-vnext.md`). Implementation on this branch will carry its own tests per `tests/AGENTS.md` and a CHANGELOG entry.

## Knowledge Updates

Plan doc committed at `docs/plans/hooks-vnext.md`. Triage evidence lives in the docs repo under `work/issue-triage-sweep/`.

## Spawn Trace

- p5268, p5269 (terra explorers) — issue triage lanes C/D
- two native sonnet reviewer lanes — issue triage lanes A/B
