## Why

Scripted (non-interactive) Claude primaries record no `harness_session_id` unless one was explicitly passed, so `--fork`/`--continue` fail; `session log` trusts the ambient `CLAUDE_CONFIG_DIR` instead of tracked metadata; session-id discovery still leans on filesystem polling after exit; and there is no first-class session discovery surface.

## Goal

A session's harness identity is captured at the source, stored as authority, and discoverable — regardless of how the primary was launched or what the ambient environment looks like later.

## Summary

Planning draft. Scope, per issue:

- **Closes #165 / Closes #166** — one fix: derive the harness session id from the materialized transcript for scripted primaries (`claude.py` currently only extracts an explicit `--session-id` arg), unblocking fork/nested seeding. Triage: same root cause, land together.
- **Closes #171** — `session log` transcript lookup falls back to tracked session metadata / canonical root instead of only live `CLAUDE_CONFIG_DIR`.
- **Closes #34** — reduce filesystem-polling reliance for session-id observation where adapters now support direct detection; Codex child-id extraction from artifacts is the remaining case.
- **Closes #7** — first-class `session list/show` discovery on top of the search machinery that already exists.

## Resulting Behavior

`meridian session` can find, show, fork, and continue any tracked session — including scripted primaries — without environment archaeology.

## Changes

#165/#166 first (identity at the source), #171 second (lookup authority), #34/#7 after.

## Work Item

issue-triage-sweep

## Verification

None yet — this is a planning draft (scoping commit only: `docs/plans/session-identity-fidelity.md`). Implementation on this branch will carry its own tests per `tests/AGENTS.md` and a CHANGELOG entry.

## Knowledge Updates

Plan doc committed at `docs/plans/session-identity-fidelity.md`. Triage evidence lives in the docs repo under `work/issue-triage-sweep/`.

## Spawn Trace

- p5268, p5269 (terra explorers) — issue triage lanes C/D
- two native sonnet reviewer lanes — issue triage lanes A/B
