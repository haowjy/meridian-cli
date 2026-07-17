## Why

**Priority: low.** Meridian abstracts over harnesses, but there is no durable record of what each harness actually supports or what model actually ran — capability gaps surface as per-incident surprises. The peer source study reinforced per-harness capability tables as a worthwhile steal.

## Goal

Capability and model fidelity become recorded artifacts, not tribal knowledge: per-harness capability limits, requested-vs-normalized-vs-reported model per run, transcript authority via official APIs, and live-probe discipline.

## Summary

Planning draft. Scope, per issue:

- **Closes #75** — generic capability-limits artifact for unsupported harness semantics (the per-harness capability table).
- **Closes #74** — record requested vs normalized vs harness-reported model per run.
- **Closes #76** — prefer official transcript/export APIs over event scraping (OpenCode report path still scrapes stream/DB).
- **Closes #73** — define harness live-probe artifacts for adapter API work.
- **Closes #63** — complete OpenCode native file delivery e2e wiring.
- **Closes #183** — audit Claude adapter `blocked_child_env_vars` completeness (only `CLAUDECODE` is blocked today).
- **Closes #256** — Cursor fast model variants (revisit; alias workaround documented).
- **Closes #22** — discover actually-available models per installed harness/provider.
- **Closes #2** — model catalog improvements (alias management, static/default direction).
- **Closes #55** — OpenCode media/vision input support (depends on an upstream capability probe — a natural first use of #73).

## Resulting Behavior

"Does harness X support Y, and what model actually ran?" is answerable from artifacts.

## Changes

#75/#73 define the artifact shapes; everything else populates or consumes them. Low urgency; good background-lane work.

## Work Item

issue-triage-sweep

## Verification

None yet — this is a planning draft (scoping commit only: `docs/plans/harness-capability-fidelity.md`). Implementation on this branch will carry its own tests per `tests/AGENTS.md` and a CHANGELOG entry.

## Knowledge Updates

Plan doc committed at `docs/plans/harness-capability-fidelity.md`. Triage evidence lives in the docs repo under `work/issue-triage-sweep/`.

## Spawn Trace

- p5268, p5269 (terra explorers) — issue triage lanes C/D
- two native sonnet reviewer lanes — issue triage lanes A/B
