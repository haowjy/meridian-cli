## Why

The drain-convergence migration (PR #368, v0.3.32) shipped with a documented tail of non-blocking convergence work: bridge scaffolding still in the tree, a composition root not fully inverted, duplicated characterization tests, one Windows CI flake, and two completion-policy questions surfaced by the peer-orchestrator source study. This PR is the container for finishing that tail while the context is fresh.

## Goal

The streaming/drain layer reaches its intended end-state: no migration scaffolding, Pi policy fully isolated from the generic drain contract, one shared test scenario builder, deterministic history reads on Windows, and an explicit (decided) timeout policy for both resident and Pi completion profiles.

## Summary

Implemented. Scope, per issue:

- **Closes #369** — fix the Windows flake: poll `_read_history_phases` with a bounded deadline instead of a single immediate read; audit sibling tests for the same publish-before-async-telemetry read race; verify the phase is late, not dropped.
- **Closes #370** — finish inverting the composition root: split `drain_teardown.py` into neutral contract vs Pi policy; inject descendant cancellation into resident cleanup via `drain_plan_factory.py`.
- **Closes #371** — delete completed-migration scaffolding (tracker/ledger façades, test-only coordinator mutation ports); resolve the residual Cleaning-phase remnant; drop the stale phase-4 TODO.
- **Closes #372** — consolidate the five overlapping Pi drain characterization test files behind one `tests/support/` scenario builder; table-ize pure priority cases; delete Phase-0 duplicates.
- **Closes #322** — moved completion/cancel precedence behind shared state
  reconciliation and re-audited the old seam requests against the coordinator/factory
  layer. The two verified-deferred residuals are tracked explicitly in #430.
- **Closes #373** — resident rearm-count budget: bound resident lifetime by number of explicit rearm extensions, composing with the existing 1800s hard cap.
- **Closes #374** — decide the Pi absolute-ceiling question (status quo / non-rearming ceiling / shared bounded-extension model) and implement or document the outcome.
- **Closes #241** — re-author the pi-rpc-quiescence smoke guide against the new drain layer, then tier it must-run vs deep diagnostics.

## Resulting Behavior

Importing the drain contract no longer pulls in Pi policy; the test suite for this layer is smaller and deterministic on Windows; resident/Pi timeout semantics are explicit and configured, not incidental.

## Changes

Delivered in sequence: #369 (deterministic race repro + bounded history-phase
polls; windows-gate CI demoted to non-blocking per POSIX-first decision) →
#322 item 3 (shared `state/reconciliation.py`, parallel lane) → #371
(scaffolding deletion, net -178 production LOC; unreachable `cleaning` phase
literal removed) → #370 (neutral `drain_teardown.py` vs `pi_drain_teardown.py`
split, factory-injected resident descendant cancellation, import isolation
verified) → #372 (Pi drain scenario builder in `tests/support/pi.py`, net -444
LOC across the five characterization files) → #373 (opt-in resident rearm-count
budget, default unlimited, `resident_rearm_count` telemetry,
`resident_rearm_budget_exhausted` outcome) + #374 (decision: status quo,
documented — `--timeout`/`MERIDIAN_TIMEOUT` is the shared opt-in absolute
ceiling) → #241 (smoke guide re-authored and tiered).

Policy decisions (user, 2026-07-17): rearm budget defaults unlimited (legit
24h+ resident sessions exist); Pi stays unbounded-while-children-live by
design; windows-gate is informational only.

#322 residuals are tracked in #430: resident nudges still call the resident
capability directly rather than the `SerializedInject` seam, and launch lifecycle
tests still seed `manager._sessions` privately.

## Work Item

issue-triage-sweep

## Verification

Per-increment and post-merge gates, all green: `uv run ruff check .`,
`uv run pytest-llm` (1526 passed, 2 skipped at final integration verify),
`uv run --extra dev python -m pyright` (0 errors). #369's fix carries a
deterministic pre-fix repro (gated async-cleanup service proving
publish-before-telemetry ordering). Increment review (G2) found no production
defects; its three test-side findings were fixed on-branch (79f4c80c). Final
whole-change review + runtime probe (G4) results recorded before draft→ready.
CHANGELOG entries per issue.

## Knowledge Updates

Plan doc committed at `docs/plans/drain-streaming-cleanup.md`. Triage evidence lives in the docs repo under `work/issue-triage-sweep/`.

## Spawn Trace

- p5268, p5269 (terra explorers) — issue triage lanes C/D
- two native sonnet reviewer lanes — issue triage lanes A/B

# Note for PR #375 (post-drain-convergence streaming cleanup)

No new issue. Audit findings [33]/[41] (thermo-nuclear audit #389) re-derive #373
(resident rearm-count budget) and #374 (Pi absolute deadline ceiling — open decision)
independently from the omnigent/peer corpus. Attach the panel's corrections to those
issues before implementation:

- **Resident rearm is not activity-driven.** `reset_deadline` fires only on an explicit
  agent signal file (`resident_drain.py:139-141,236,274-283` via `consume_resident_signals`);
  evidence refresh does NOT extend the deadline. The window (default 3300 s) is absolute per
  arm. #373's gap is solely an uncapped *count* of deliberate rearms — and since rearm is
  user/agent-directed, the budget value is a product decision, not an arbitrary counter.
- **Pi windows are anchored, not sliding.** `reset_deadline=True` recomputes `min()` over
  windows anchored per notification (`pi_work_ledger.py:146-168`) and once per child wave
  (`pi_completion_profile.py:524-535`); ordinary descendant disk evidence does not postpone
  an armed window. Unboundedness comes from *successive* waves/notifications re-anchoring
  new windows (parent `turn_active` clears the timer at `pi_completion_profile.py:309-315`).
  #374's ceiling must be a separately modeled total-lifetime clock — do not overload the
  notification/child-wave deadlines.
- **An absolute ceiling already exists, opt-in.** `--timeout`/`MERIDIAN_TIMEOUT` arms a
  non-renewing outer attempt timer once (`streaming_runner.py:739-786`); default `None`.
  Both issues are about the *default* being single-layered, and #374's decision can be
  framed as "what default for the existing budget mechanism," not new machinery.
