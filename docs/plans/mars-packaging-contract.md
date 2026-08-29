## Why

Eleven open issues are facets of one under-specified contract between meridian-cli and Mars: authoring vocabulary, profile model overrides, frontmatter fidelity across ecosystems, JSON contract versioning, binary discovery, prompt-stage field naming, and subprocess cost. They keep being filed separately because the contract was never written down.

## Goal

One design pass that writes the meridian↔Mars contract, then mechanical follow-through per issue. Design-first: most members need the written contract before code.

## Summary

Planning draft. Scope, per issue:

- **Closes #118** — standardize Mars authoring vocabulary across mars.toml, docs, CLI (foundational — do first).
- **Closes #119** — formalize the agent-profile `models` override table.
- **Closes #345** — cross-ecosystem frontmatter fidelity: lift layer, canonical schema gaps, per-import overrides (mars-agents side has lower.rs only).
- **Closes #90** — version the mars JSON contract; centralize binary discovery (currently duplicated in `ops/mars.py` and `catalog/model_aliases.py`).
- **Closes #117** — hook compilation to harness-native formats (scope/ownership decision).
- **Closes #116** — harness-specific prompt variant compilation (decision).
- **Closes #71** — bidirectional agent-profile translation (canonical IR question; depends on #345's lift layer).
- **Closes #163** — Mars-side agent/profile compile API exploration.
- **Closes #72** — clarify prompt field naming across launch lifecycle types (`SpawnRequest.prompt` vs `ResolvedLaunchSpec.prompt`); fold the resume-vs-continue vocabulary distinction from the source study into the same naming pass.
- **Closes #158** — document the bundled-binary-vs-PATH benchmarking gotcha (or emit a note when they diverge).
- **Closes #156** — eliminate subprocess overhead for model resolution (SDK/manifest path; <50ms target).

## Resulting Behavior

Authoring vocabulary, override semantics, and the JSON contract are versioned and written; the remaining members become mechanical.

## Changes

Spans this repo and sibling `../mars-agents` + prompt packages — read each repo's AGENTS.md before touching it. Sequencing: #118 → #119/#90 → #345/#71 → the rest.

## Work Item

issue-triage-sweep

## Verification

None yet — this is a planning draft (scoping commit only: `docs/plans/mars-packaging-contract.md`). Implementation on this branch will carry its own tests per `tests/AGENTS.md` and a CHANGELOG entry.

## Knowledge Updates

Plan doc committed at `docs/plans/mars-packaging-contract.md`. Triage evidence lives in the docs repo under `work/issue-triage-sweep/`.

## Spawn Trace

- p5268, p5269 (terra explorers) — issue triage lanes C/D
- two native sonnet reviewer lanes — issue triage lanes A/B
