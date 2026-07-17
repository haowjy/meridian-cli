## Why

Triage validated a set of small, independent CLI correctness footguns — each cheap, each real, none design-blocked. Bundled for scheduling, not because they share code.

## Goal

The CLI stops doing surprising things in common flows.

## Summary

Planning draft. Scope, per issue:

- **Closes #321** — unknown subcommand-like token (`meridian spawn list-agents`) errors instead of silently launching as a prompt.
- **Closes #299** — primary dry-run stops creating project identity before validation (read-only startup class for dry-run).
- **Closes #283** — `@latest` sort key: non-numeric ids no longer sort into the always-last bucket and win over real spawns.
- **Closes #334** — params.json records the bound (child) work dir, not the prepare-time parent dir, for ambient child spawns.
- **Closes #313** — unify `--task-dir` across commands; `session log` is the confirmed missing surface.
- **Closes #198 / Closes #196** — one fix: `work start` activation is visible to later bare-CLI queries (`work current`, `meridian context`) or the help text states session-scoping honestly; `set_session_work_attachment` currently no-ops without a live session.
- **Closes #113** — sparse explicit JSON projections for high-noise outputs (`SpawnDetailOutput.report_body`, hook stdout/stderr).
- **Closes #9** — separate `work done` from archival so completion isn't destructive to the scratch dir.

## Resulting Behavior

Typos don't launch spawns, dry-runs don't mutate, `@latest` means latest, and `work start` means started.

## Changes

All S–M, independently mergeable; land in any order. Good lane for fast, well-scoped implementation spawns.

## Work Item

issue-triage-sweep

## Verification

None yet — this is a planning draft (scoping commit only: `docs/plans/cli-ux-correctness.md`). Implementation on this branch will carry its own tests per `tests/AGENTS.md` and a CHANGELOG entry.

## Knowledge Updates

Plan doc committed at `docs/plans/cli-ux-correctness.md`. Triage evidence lives in the docs repo under `work/issue-triage-sweep/`.

## Spawn Trace

- p5268, p5269 (terra explorers) — issue triage lanes C/D
- two native sonnet reviewer lanes — issue triage lanes A/B
