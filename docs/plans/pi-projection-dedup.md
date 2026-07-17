## Why

**Priority: low.** Two leftovers from the Pi surface work: the RPC and native-TUI projections duplicate ~500 lines of collision-detection helpers verbatim, and the managed-bash extension's two-bucket lifecycle dispatch was structurally split but never verified complete.

## Goal

One copy of the Pi projection scaffolding; managed-bash's dispatch model verified or finished.

## Summary

Planning draft. Scope, per issue:

- **Closes #240** — extract shared `_pi_projection_common` for the collision-detection helpers duplicated across `project_pi_rpc.py` / `project_pi_native_tui.py`.
- **Closes #238** — verify/finish the two-bucket fire-and-forget dispatch in `pi_runtime/extensions/managed-bash/` (structural split already landed; #264 closed on that basis).

## Resulting Behavior

Pi projection changes are made once; managed-bash lifecycle semantics match the documented model.

## Changes

Small, low-risk, self-contained — a good warm-up or fill-in PR.

## Work Item

issue-triage-sweep

## Verification

None yet — this is a planning draft (scoping commit only: `docs/plans/pi-projection-dedup.md`). Implementation on this branch will carry its own tests per `tests/AGENTS.md` and a CHANGELOG entry.

## Knowledge Updates

Plan doc committed at `docs/plans/pi-projection-dedup.md`. Triage evidence lives in the docs repo under `work/issue-triage-sweep/`.

## Spawn Trace

- p5268, p5269 (terra explorers) — issue triage lanes C/D
- two native sonnet reviewer lanes — issue triage lanes A/B
