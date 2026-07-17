## Why

Three explicitly-parked design decisions all touch the same surface — who owns environment variables and project identity: child-env recomposition vs consuming `final_env`, the `_MERIDIAN_*` internal/public prefix convention, and repo-local `.meridian/` vs `meridian.toml` identity. Blast radius spans prompt packages, hooks, pi_runtime, and mars-agents, so they deserve one coordinated pass, not three drive-bys.

## Goal

One written decision set: env-var namespace convention, single child-env composition point, and project identity in `meridian.toml` — then mechanical migration.

## Summary

Planning draft. Scope, per issue:

- **Closes #361** — child-env ownership: connections consume `final_env` instead of recomposing from `os.environ` (5 connection adapters still call `inherit_child_env` themselves).
- **Closes #336** — decide the `_MERIDIAN_*` internal vs public env convention and split `ALLOWED_CHILD_ENV_KEYS` accordingly.
- **Closes #341** — deprecate repo-local `.meridian/`: move project identity into `meridian.toml`.

## Resulting Behavior

Env composition happens once, the public env surface is deliberate, and project identity lives in the config file users already edit.

## Changes

Design-first (`@design-lead` pass), then migration. #361 and #336 share the namespace decision; #341 is separable but benefits from the same review. Cross-repo impact: prompt packages and mars-agents read MERIDIAN_* vars.

## Work Item

issue-triage-sweep

## Verification

None yet — this is a planning draft (scoping commit only: `docs/plans/env-identity-ownership.md`). Implementation on this branch will carry its own tests per `tests/AGENTS.md` and a CHANGELOG entry.

## Knowledge Updates

Plan doc committed at `docs/plans/env-identity-ownership.md`. Triage evidence lives in the docs repo under `work/issue-triage-sweep/`.

## Spawn Trace

- p5268, p5269 (terra explorers) — issue triage lanes C/D
- two native sonnet reviewer lanes — issue triage lanes A/B
