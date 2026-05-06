# Design overview: generic micro-CT analysis toolkit

This design package is the entry point for the active micro-CT efficiency work. It consolidates the architect spawn's seed-curation drafts into an authoritative implementation design for a generic micro-CT analysis toolkit.

## Authoritative path

Read these first, in order:

1. [Architecture](architecture/architecture.md) — authoritative source for module boundaries, APIs, agent topology, and integration rules.
2. [Behavioral spec](spec/behavioral-spec.md) — EARS-MCT requirements that implementation and verification must satisfy.
3. [Refactors](refactors.md) — preparatory structural work and sequencing dependencies.
4. [Feasibility](feasibility.md) — probe evidence, risk assessment, dependency constraints, and effort estimate.

## Architecture support material

- [Agent architecture](architecture/agent-architecture.md) — progressive-narrowing and agent responsibility view that complements the authoritative architecture.
- [Architecture package overview](architecture/overview.md) — local navigation for architecture documents.

## Preserved architect-spawn drafts

The following documents are preserved for rationale and audit history. They are not authoritative when they differ from [Architecture](architecture/architecture.md) or the [Behavioral spec](spec/behavioral-spec.md):

- [Seed curation options](architecture/seed-curation-options.md) — alternatives considered and tradeoff matrix.
- [Seed curation decision](architecture/seed-curation-decision.md) — original decision record selecting the dedicated seed-curator substage.
- [Seed curation target architecture](architecture/seed-curation-target-architecture.md) — earlier target-state sketch; API names and module details have been superseded.

## Design center

The chosen design keeps deterministic computation in stable tool boundaries and moves the high-iteration anatomical judgment loop into the `slice-examination-loop` skill. The critical behavior is visual seed placement with point-marker watershed, not geometry-derived flood-fill seeds. The skill is loaded by the segmenter directly — no spawn overhead, volume stays in the same kernel context — and is reusable by any agent that needs visual slice examination for spatial decisions.
