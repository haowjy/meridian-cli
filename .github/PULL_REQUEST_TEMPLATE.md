<!-- Describe the PR's final state, not its history. Give reviewers enough
     context to understand the change. -->

## Why

<!-- State the problem or opportunity and why it matters. Include before-state
     evidence when it exists: failing output, timings, a reproduction. -->

## Goal

<!-- State what must be true after merge. -->

## Summary

<!-- Briefly map the solution and key tradeoffs; omit commit logs and diff stats. -->

## Diff

<!-- Report file/line totals excluding generated files (lockfiles, built Pi
     extension bundles), then give non-overlapping area totals that add up.
     Report on-disk state and schema changes under State / Schema Changes. -->

- Total: N files, +N / -N
- Breakdown:
  - Area: N files, +N / -N — what changed

## Before / After

<!-- List each concrete change as its own item; do not collapse the PR into one
     paragraph. Label a bug fix explicitly ("Bug fix: ...") so it doesn't blend
     into feature description. For a bug fix or changed behavior, give Before
     and After; keep Before brief or omit it when no meaningful prior state
     exists. For a new capability, describe After only. Prefer CLI commands and
     their output over prose. -->

- **Bug fix: <what was broken>.** Before: ... After: ...
- **<capability or behavior change>.** After: ...

## Code Changes

<!-- Group production-code additions, refactors, and deletions by area; include
     key paths and rationale. Aim to lower net-new code over time by simplifying,
     refactoring, and removing duplication/dead paths—not by shrinking this diff.
     Explain substantial additions. Track temporary code or cleanup deferred for
     delivery speed/product clarity, with its reason and removal trigger. -->

- Added:
- Refactored:
- Deleted:

## State / Schema Changes

<!-- Remove if none. Covers on-disk authority files (spawn state.json,
     sessions.jsonl, spawn artifacts), rebuildable SQLite projections and their
     schema versions (history index, search index), and config/env contracts.
     For each: old → new shape, how existing user state upgrades (automatic
     migration, `meridian doctor`, index rebuild), what an older build still
     running against the same runtime sees, and how to roll back. Diagram
     changed relationships in Mermaid when it helps. -->

## Work Item

<!-- Link the issue, work item, design, or plan; otherwise say this was direct
     maintenance. -->

## Testing

<!-- List tests added/refactored/deleted and the contract or risk each protects
     (or why removal is safe). Don't add tests for volume or coverage; consider
     deleting redundant tests and development scaffolding. -->

- Added:
- Refactored:
- Deleted:

## Verification

<!-- Describe how to exercise changed behavior, then record what was tested and
     observed. Include setup, steps, expected/actual results, and workflows a
     probe covered. Name which harnesses (claude, codex, cursor, opencode, pi)
     were exercised live and which only through fakes. Runtime probes run
     against a copied or scratch runtime (`_MERIDIAN_RUNTIME_DIR`), never a real
     one. If no manual path exists, say why. For performance changes, report
     comparable numbers with metric, method, environment/workload, and
     baseline/result; omit benchmarks when performance is unaffected. -->

- Gates (`scripts/preflight.sh full`, pyright, extension build/tests):
- Workflow/probe:
- Setup and steps:
- Expected / observed:
- Performance (when relevant):

## Deferred

<!-- List deferred work and its tracking home: issue for cross-cutting work;
     nearest .context/TODO or .context/FUTURE for local work. Temporary code or
     cleanup deferred for speed/product clarity needs a removal trigger. Say if none. -->

- [ ] Tracking home for each deferral; removal/cleanup trigger when applicable, or none

## Knowledge Updates

<!-- List durable guidance updated (colocated AGENTS.md / .context/, docs/, KB),
     or say why none was needed. -->

- [ ] `.context/` / docs / KB updates are included, or not needed

## Spawn Trace

<!-- List delegated agents and roles, or say the work was done directly:
     - p123 coder — implemented catalog filtering
     - p124 reviewer — structural review
-->

## Release Label Guide

Set one `release:*` label on this PR:

- `release:patch` / `release:stable` — next stable **patch** release after merge
- `release:minor` — next stable **minor** release after merge
- `release:major` — next stable **major** release after merge
- `release:rc` — next **prerelease (RC)** after merge
- `release:skip` — no release for this merge

No `release:*` label defaults to a prerelease (RC). Unknown `release:*` labels also default to RC.

## Post-Merge Automation

After merge to `main`, CI (`.github/workflows/release-on-merge.yml`) will:

1. Read the PR release label
2. Skip only when `release:skip` is present (no label defaults to RC)
3. Compute the next stable or RC version from existing `v*` tags
4. Update `src/meridian/__init__.py` + promote `CHANGELOG.md` `[Unreleased]`
5. Commit `Release X.Y.Z`, create/push `vX.Y.Z`
6. Run `.github/workflows/publish-pypi.yml`

## Cleanup

After merge, clean merged worktrees with:

```bash
scripts/prune-worktrees.sh
```
