# Requirements — launch boundary observability

## Problem statement

Meridian can leave background child spawns stuck in `running` when they fail before the detached worker takes over. The current state artifacts are insufficient to distinguish whether the detached launcher failed to exec, crashed before writing logs, wrote elsewhere, or whether the parent recorded a non-authoritative PID. Reaper/doctor can also be fooled when a stale numeric PID now belongs to an unrelated live process.

## Desired outcome

When a background spawn fails or stalls between parent launch and worker takeover, Meridian should leave durable evidence that explains what happened and gives the reaper enough information to classify the row safely.

## Requirements

- Record durable launch-boundary observations for background spawns before and during detached worker takeover.
- Separate launcher identity from authoritative worker/runner identity in logs/artifacts; do not make the existing dashboard more misleading.
- Capture enough fields to diagnose pre-worker ghosts: launch command/process identity where safe, parent-observed PID, worker takeover timestamp, worker PID, harness session id when available, and failure/exception details.
- Preserve crash-only behavior: writes must be atomic or append-safe and tolerate truncation.
- Preserve Windows support; avoid POSIX-only assumptions except behind existing platform/process abstractions.
- Add/adjust tests for the observability behavior and for stale pre-worker ghost classification where appropriate.
- Do not delete or revert unrelated user/agent changes.

## Non-goals

- Do not kill or prune live spawns as part of this implementation.
- Do not redesign the full spawn lifecycle state machine unless needed for minimal observability.
- Do not require git for core behavior.
