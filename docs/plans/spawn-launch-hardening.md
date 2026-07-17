## Why

PR #320 bounded the post-connect event stream (BackendLivenessPolicy), but the pre-connect window — subprocess start through handshake — is still unbounded, retry attempts still erase first-attempt diagnostics, and two concrete launch-path defects remain (AF_UNIX socket path length, port-selection TOCTOU). #37's mid-turn Codex orphan case needs the preserved diagnostics to be diagnosable at all.

## Goal

Every phase of a streaming spawn launch is bounded and diagnosable: a startup watchdog covers the pre-connect phase, failed-attempt artifacts survive retries, and known launch-path defects are closed.

## Summary

Planning draft. Scope, per issue:

- **Closes #235** — add a startup-phase watchdog around `start_spawn`/`_start_connection` (the "2h18m wedged before any liveness signal" case); stop truncating first-attempt logs/artifacts on retry (`streaming_runner.py:1107-1112`).
- **Closes #168** — bound `control.sock` path length (hash/shorten under long runtime roots) in `spawn_manager.py`; absorbs closed #174.
- **Closes #201** — close the `_find_free_port` bind-close-reuse TOCTOU in the OpenCode launch path (bind-and-hold or retry-on-EADDRINUSE as the contract).
- **Closes #37** — make Codex runner mid-turn loss diagnosable: persist enough runner/backend evidence that an `orphan_run` reconciliation can say *why* (depends on #235's preserved diagnostics).

## Resulting Behavior

A spawn that cannot launch fails fast with intact first-attempt evidence instead of wedging silently or retrying over its own diagnostics.

## Changes

#235 is the keystone; #37 consumes it. #168/#201 are independent, small, first-mergeable.

## Work Item

issue-triage-sweep

## Verification

None yet — this is a planning draft (scoping commit only: `docs/plans/spawn-launch-hardening.md`). Implementation on this branch will carry its own tests per `tests/AGENTS.md` and a CHANGELOG entry.

## Knowledge Updates

Plan doc committed at `docs/plans/spawn-launch-hardening.md`. Triage evidence lives in the docs repo under `work/issue-triage-sweep/`.

## Spawn Trace

- p5268, p5269 (terra explorers) — issue triage lanes C/D
- two native sonnet reviewer lanes — issue triage lanes A/B
