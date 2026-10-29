<!-- Describe the PR's final state, not its history. A reviewer should get the
     purpose, the decisions, the surrounding work and the full scope from the
     sections up to Summary, before any detail. -->

## Changelog

<!-- CI validates this section; the release job renders it into CHANGELOG.md
     under the new version heading and uses it as the GitHub Release body. Do
     not edit CHANGELOG.md in this PR. Use `### Upgrade notes`, `### Added`,
     `### Changed`, `### Deprecated`, `### Removed`, `### Fixed`, or
     `### Security` blocks with `- ` bullets (continuation lines are fine), or
     replace the whole section with one line: `None. <reason nothing a user or
     developer notices changed>`. Write one line per upgrade note, ending in an
     absolute link to the guide section: `- ... See [what changed](https://github.com/haowjy/meridian-cli/blob/main/docs/upgrading.md#anchor).`
     The guide carries the detail; the changelog entry links to it. Absolute
     links resolve in both the PR body and the GitHub Release body. -->

### Fixed
- <what a user now sees that was broken before>

## Why

<!-- State the problem or opportunity and why it matters. Include before-state
     evidence when it exists: failing output, timings, a reproduction. -->

## Core design decisions

<!-- Name the core design decisions behind the change: the underlying
     problems, not just their symptoms. For each, say what was wrong, what this
     PR decides, and what it rules out. Quote the owner's rulings where they
     set a decision. Write "None." when the change involves no design choice. -->

1. **<decision>.** <the problem>. <what this PR decides>. Ruled out: <alternative and why>.

## Goal

<!-- Start with the long-term goal this PR serves: the direction it moves
     toward, even when this PR is one step of it. Write "None." when the PR
     stands alone. Then state what must be true after merge. -->

**Long term.** <the direction this PR moves toward, or "None.">

**This PR.**

- <what must be true after merge>

## Work Item

<!-- Name the work item, issue, design, or plan this PR delivers; otherwise
     say this was direct maintenance. Under it, list other related work: PRs
     this builds on or unblocks, and issues it fixes or prepares for. Deferred
     work goes under Deferred. Write "None." when nothing is related. -->

<work item, issue, design, or plan, or "Direct maintenance.">

**Related work.**

- #N: <how it relates>

## Summary

<!-- List every part of the PR: each change a reviewer could ask "why is this
     here?" about, including follow-on fixes, safety work the main change
     forced, and anything added mid-review. For each, say what it does and why
     it is in this PR. A change missing from this list should not be in the
     diff. Keep it current as parts are added. -->

1. **<part>.** <what it does>. Why it's here: <reason>.

## Diff

<!-- One row per Summary part, same numbers. Part sizes are approximate when
     parts share a file. The Total row is exact: `git diff --numstat` against
     the base, excluding generated files (lockfiles, built Pi extension
     bundles). -->

| # | Part | Source | Tests | Docs |
|---|---|---|---|---|
| 1 | <part> | +N / -N | +N / -N | +N / -N |
| | **Total (N files)** | **+N / -N** | **+N / -N** | **+N / -N** |

## Before / After

<!-- List each concrete change as its own item; do not collapse the PR into one
     paragraph. Label a bug fix explicitly ("Bug fix: ...") so it doesn't blend
     into feature description. For a bug fix or changed behavior, give Before
     and After; keep Before brief or omit it when no meaningful prior state
     exists. For a new capability, describe After only. Prefer CLI commands and
     their output over prose. -->

- **Bug fix: <what was broken>.** Before: ... After: ...
- **<capability or behavior change>.** After: ...

## End-to-End Walkthrough

<!-- Take a reviewer through the new or changed behavior from the user's side,
     when there is something to walk through. A UI or TUI change gets
     screenshots or a short recording with numbered steps and the before/after
     state. A CLI, config, library or docs change gets a numbered text
     walkthrough: copy-pasteable commands and their real output, or
     before/after files. Where behavior changed, show the before state on main
     and the after state on this branch. If nothing is runnable or visible,
     write "Not applicable" and say what check replaces it. -->

1. Setup: ...
2. ...
3. Expected: ... Observed: ...

## Code Changes

<!-- For each part in the Summary, explain how it works and name its key
     paths. Explain substantial additions. Aim to lower net-new code over time
     by simplifying, refactoring, and removing duplication/dead paths—not by
     shrinking this diff. -->

**1. <part>.** <how it works and why it is built this way>

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

## Testing

<!-- List tests added/refactored/deleted and the contract or risk each protects
     (or why removal is safe). Don't add tests for volume or coverage; consider
     deleting redundant tests and development scaffolding. -->

- Added:
- Refactored:
- Deleted:

## Verification

<!-- Record what the walkthrough, the probes and the gates showed: expected
     against observed, which harnesses (claude, codex, cursor, opencode, pi)
     were exercised live and which only through fakes, and any workflow a probe
     covered. Runtime probes run against a copied or scratch runtime
     (`_MERIDIAN_RUNTIME_DIR=<dir>`, honoured at any spawn depth, including
     inside a spawn; confirm with `spawn --dry-run` that `Write root` names it;
     `-C` to another project drops it), never a real one. For performance
     changes, report comparable numbers with metric, method,
     environment/workload, and baseline/result; omit benchmarks when
     performance is unaffected. -->

- Gates (`scripts/preflight.sh full`, pyright, extension build/tests):
- Live check (expected / observed):
- Not exercised live:
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

Set one `release:*` label on this PR. The bump tracks breakage, not size: a
change that breaks no existing behaviour, config, or on-disk state is a patch,
however much it adds (`docs/releasing.md`).

- `release:patch` / `release:stable` — next stable **patch** release after merge
- `release:minor` — next stable **minor** release after merge; breaks existing behaviour, config, or state
- `release:major` — next stable **major** release after merge; breaks existing behaviour, config, or state
- `release:rc` — next **prerelease (RC)** after merge
- `release:skip` — no release for this merge

No `release:*` label defaults to a prerelease (RC). Unknown `release:*` labels also default to RC.

## Post-Merge Automation

CI releases per your label after merge; see
[what CI does](https://github.com/haowjy/meridian-cli/blob/main/docs/releasing.md#what-ci-does)
for the steps.

## Cleanup

After merge, clean merged worktrees with:

```bash
scripts/prune-worktrees.sh
```
