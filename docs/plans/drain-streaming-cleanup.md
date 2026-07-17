## Why

The drain-convergence migration (PR #368, v0.3.32) shipped with a documented tail of non-blocking convergence work: bridge scaffolding still in the tree, a composition root not fully inverted, duplicated characterization tests, one Windows CI flake, and two completion-policy questions surfaced by the peer-orchestrator source study. This PR is the container for finishing that tail while the context is fresh.

## Goal

The streaming/drain layer reaches its intended end-state: no migration scaffolding, Pi policy fully isolated from the generic drain contract, one shared test scenario builder, deterministic history reads on Windows, and an explicit (decided) timeout policy for both resident and Pi completion profiles.

## Summary

Planning draft. Scope, per issue:

- **Closes #369** — fix the Windows flake: poll `_read_history_phases` with a bounded deadline instead of a single immediate read; audit sibling tests for the same publish-before-async-telemetry read race; verify the phase is late, not dropped.
- **Closes #370** — finish inverting the composition root: split `drain_teardown.py` into neutral contract vs Pi policy; inject descendant cancellation into resident cleanup via `drain_plan_factory.py`.
- **Closes #371** — delete completed-migration scaffolding (tracker/ledger façades, test-only coordinator mutation ports); resolve the residual Cleaning-phase remnant; drop the stale phase-4 TODO.
- **Closes #372** — consolidate the five overlapping Pi drain characterization test files behind one `tests/support/` scenario builder; table-ize pure priority cases; delete Phase-0 duplicates.
- **Closes #322** — remaining live item: `state/managed_primary.py` imports private `reaper._completion_or_cancel_decision`; re-audit the old seam asks against the new coordinator/factory layer (items 1–2 are superseded, item 4 partially done).
- **Closes #373** — resident rearm-count budget: bound resident lifetime by number of explicit rearm extensions, composing with the existing 1800s hard cap.
- **Closes #374** — decide the Pi absolute-ceiling question (status quo / non-rearming ceiling / shared bounded-extension model) and implement or document the outcome.
- **Closes #241** — re-author the pi-rpc-quiescence smoke guide against the new drain layer, then tier it must-run vs deep diagnostics.

## Resulting Behavior

Importing the drain contract no longer pulls in Pi policy; the test suite for this layer is smaller and deterministic on Windows; resident/Pi timeout semantics are explicit and configured, not incidental.

## Changes

Suggested sequencing: #369 first (unblocks CI trust), then #371 → #370 (scaffolding out before inversion), #372 riding the same test pass as #369, then #373/#374 (policy), #322 and #241 last.

## Work Item

issue-triage-sweep

## Verification

None yet — this is a planning draft (scoping commit only: `docs/plans/drain-streaming-cleanup.md`). Implementation on this branch will carry its own tests per `tests/AGENTS.md` and a CHANGELOG entry.

## Knowledge Updates

Plan doc committed at `docs/plans/drain-streaming-cleanup.md`. Triage evidence lives in the docs repo under `work/issue-triage-sweep/`.

## Spawn Trace

- p5268, p5269 (terra explorers) — issue triage lanes C/D
- two native sonnet reviewer lanes — issue triage lanes A/B
