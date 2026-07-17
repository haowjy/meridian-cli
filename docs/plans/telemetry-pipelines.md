## Why

**Priority: low.** Telemetry is local-only today. The v2 (error reporting) and v3 (usage/feature tracking) remote pipelines are designed-but-unbuilt, and the reader duplicates its truncation-tolerant JSONL parsing in two places — worth extracting before any pipeline builds on it.

## Goal

A shared JSONL parsing core, then the remote error-reporting pipeline, then usage tracking.

## Summary

Planning draft. Scope, per issue:

- **Closes #139** — extract the shared truncation-tolerant JSONL line parser from `telemetry/reader.py` (`read_events`/`tail_events` currently duplicate it). Prep step.
- **Closes #141** — v2: error-reporting pipeline, local telemetry → remote upload.
- **Closes #142** — v3: feature/usage tracking pipeline to remote.

## Resulting Behavior

Errors and usage can be reported off-box (opt-in), built on one parsing core.

## Changes

#139 is mergeable immediately; #141/#142 need a transport/privacy decision (opt-in model, endpoint ownership) before code.

## Work Item

issue-triage-sweep

## Verification

None yet — this is a planning draft (scoping commit only: `docs/plans/telemetry-pipelines.md`). Implementation on this branch will carry its own tests per `tests/AGENTS.md` and a CHANGELOG entry.

## Knowledge Updates

Plan doc committed at `docs/plans/telemetry-pipelines.md`. Triage evidence lives in the docs repo under `work/issue-triage-sweep/`.

## Spawn Trace

- p5268, p5269 (terra explorers) — issue triage lanes C/D
- two native sonnet reviewer lanes — issue triage lanes A/B
