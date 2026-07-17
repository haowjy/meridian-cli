## Why

Three independently-filed issues describe one architectural gap: every spawn-store read path scales with total store size (full `state.json` scans, whole-file `history.jsonl` reads), and there is no derived index or canonical event stream to read instead. A fourth issue shows the store boundary accepts raw `str` paths, pushing normalization to call sites.

## Goal

One coherent state-store design pass: a validated, rebuildable derived index over spawn state, bounded history reads, a canonical per-spawn event log, and typed path handling at the store boundary — consistent with the files-as-authority + crash-only constraints.

## Summary

Planning draft. Scope, per issue:

- **Closes #359** — history.jsonl: stop unconditional whole-file `read_bytes()` on writer open; add segmentation/checkpointing so growth is bounded. (Triage: high priority — cost paid on every writer open.)
- **Closes #360** — filtered spawn listing: stop O(N) `state.json` reads; filter via the derived index.
- **Closes #274** — define the core child-spawn index contract (`children.json` or equivalent); `pi_subspawn_tracker.py` is a natural first consumer.
- **Closes #129** — canonical per-spawn `events.jsonl`; stop deriving spawn state from raw log heuristics.
- **Closes #211** — normalize path fields at the state-store boundary (`start_spawn` takes `Path`, store owns `.as_posix()`), replacing per-call-site normalization; this also hardens the POSIX path-in-detail contract established in #368.

## Resulting Behavior

Spawn listing and history access cost is bounded by result size, not store size; state derivation has one authority; path form is decided in exactly one place.

## Changes

These share one design shape (derived, rebuildable artifacts over authoritative files) and must be sequenced as one design pass — #274's contract first, #129/#360/#359 building on it, #211 independent and first-mergeable.

## Work Item

issue-triage-sweep

## Verification

None yet — this is a planning draft (scoping commit only: `docs/plans/state-store-scaling.md`). Implementation on this branch will carry its own tests per `tests/AGENTS.md` and a CHANGELOG entry.

## Knowledge Updates

Plan doc committed at `docs/plans/state-store-scaling.md`. Triage evidence lives in the docs repo under `work/issue-triage-sweep/`.

## Spawn Trace

- p5268, p5269 (terra explorers) — issue triage lanes C/D
- two native sonnet reviewer lanes — issue triage lanes A/B
