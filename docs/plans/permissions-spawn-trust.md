## Why

One concrete high-priority defect (Claude `--agents` payload drops tool restrictions — delegated agents run unrestricted) sits inside a broader undecided trust model: runtime permission enforcement scope, permission propagation up the spawn tree, drift-to-wrong-task spawns finalizing as succeeded, and the explicit-wait lifecycle.

## Goal

Fix the concrete permission projection bug now; decide (and document) the spawn-tree trust model before more code accretes around the ambiguity.

## Summary

Planning draft. Scope, per issue:

- **Closes #35** — serialize tool restrictions into the `--agents` payload (`harness/claude.py` currently sends only description+prompt). Concrete, high, do first.
- **Closes #315** — consolidate native-agent authority: one shared derivation for mars.toml intent vs realized-result permission gating.
- **Closes #21** — decide runtime permission resolution/enforcement scope (may resolve as "projection-only by design" — that is a valid outcome).
- **Closes #192** — permission-request propagation up the spawn tree (design; current auto-deny is the documented mitigation).
- **Closes #231** — wrong-task-success guard: a spawn that drifted to an unrelated task should not finalize `succeeded` unchallenged.
- **Closes #224** — run-until-quiescent lifecycle as the successor to explicit `spawn wait`.
- **Closes #6** — auto-review/smoke-after-spawn: decide whether this is orchestration policy (out of scope per coordination doctrine) or a hook recipe; document either way.
- **Closes #41** — LLM-driven self-termination nudges: adjudicate against the simplest-orchestration boundary; likely wontfix-by-doctrine, recorded as a decision.

## Resulting Behavior

Delegated agents actually carry their restrictions; the trust/lifecycle questions have written answers instead of open ambiguity.

## Changes

#35 and #315 are code; the rest are decision-first and may close as documented decisions rather than features. That is expected for this container.

## Work Item

issue-triage-sweep

## Verification

None yet — this is a planning draft (scoping commit only: `docs/plans/permissions-spawn-trust.md`). Implementation on this branch will carry its own tests per `tests/AGENTS.md` and a CHANGELOG entry.

## Knowledge Updates

Plan doc committed at `docs/plans/permissions-spawn-trust.md`. Triage evidence lives in the docs repo under `work/issue-triage-sweep/`.

## Spawn Trace

- p5268, p5269 (terra explorers) — issue triage lanes C/D
- two native sonnet reviewer lanes — issue triage lanes A/B
