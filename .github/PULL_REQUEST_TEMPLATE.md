<!-- Describe the PR's final state, not its history. A reader should learn what
     changes for users, why, what it looks like in use, what it costs and risks,
     and how we know it works, in that order, before any detail. Delete a
     section's placeholder text; write "None." with a reason where a section
     does not apply. -->

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

<!-- The problem or opportunity and why it matters, with before-state evidence
     when it exists: failing output, timings, a reproduction. Then what must be
     true after merge, and the longer-term direction when this PR is one step
     of it. -->

## Examples

<!-- Walk through each behavior change as a user meets it: the commands, then
     the output before and after (trimmed to the lines that show the change;
     placeholders for scratch paths and IDs), with one sentence naming the
     difference. One block per change. For a new capability, show After only.
     Run the examples on a scratch runtime, never a real one. -->

**<behavior change>.**

```console
$ meridian <command>
```

Before: `<output>` · After: `<output>`

## Benchmarks

<!-- Every command or workflow whose speed this PR could change. State the
     machine, workload, build commits and method (runs, warm/cold, statistic)
     once, above the table; give the range when runs vary. Write
     "None. <why performance is unaffected>" when nothing on a measured path
     changed. -->

<machine · workload · method>

| Command | Before | After | Change |
|---|---:|---:|---:|
| `<command>` | <N> ms | <N> ms | <±N%> |

## Design decisions

<!-- The decisions behind the change: the underlying problem, not only its
     symptom. For each, say what was wrong, what this PR decides, and what it
     rules out. Quote the owner's rulings where they set a decision. Write
     "None." when the change involves no design choice. -->

1. **<decision>.** <the problem>. <what this PR decides>. Ruled out: <alternative and why>.

## Changes

<!-- Every part of the PR: each change a reviewer could ask "why is this here?"
     about, including follow-on fixes and anything added mid-review. A change
     missing from this table should not be in the diff. Line counts come from
     `git diff --numstat` against the base, excluding generated files
     (lockfiles, built Pi extension bundles); parts sharing a file are
     approximate, the total is exact. Below the table, explain how a part works
     only where the diff alone would not make it clear. -->

| # | Part | Why it's here | Key paths | Source | Tests | Docs |
|---|---|---|---|---|---|---|
| 1 | <part> | <reason> | `<path>` | +N / -N | +N / -N | +N / -N |
| | **Total (N files)** | | | **+N / -N** | **+N / -N** | **+N / -N** |

## Compatibility and risk

<!-- What breaks or changes for existing users, config and scripts; on-disk
     state and index changes (old → new shape, how existing state upgrades,
     what an older build on the same runtime sees, how to roll back); and where
     a reviewer's time pays off most (the two or three riskiest files or
     behaviors). Write "None." with a reason for a change that touches none of
     these. -->

- Breaking / changed for users:
- State and upgrade:
- Older builds on the same runtime:
- Rollback:
- Where to look:

## Verification

<!-- Plain text: which behaviors were checked on the real system and how
     (harnesses live vs through fakes, scratch runtimes); tests added or deleted
     and the contract or risk each protects; what was not exercised and why;
     the gates in one line. -->

- Checked live:
- Tests:
- Not exercised:
- Gates (`scripts/preflight.sh full`, pyright, extension build/tests):

## Deferred

<!-- Deferred work and its tracking home: an issue for cross-cutting work; the
     nearest .context/TODO or .context/FUTURE for local work. Temporary code
     needs a removal trigger. Say if none. -->

- [ ] <deferral> — <tracking home>

## References

<!-- The work item, design or plan this PR delivers (or "Direct maintenance."),
     related PRs and issues and how they relate, durable docs updated
     (AGENTS.md, .context/, docs/, KB), and the delegated agents and their
     roles. -->

- Work item:
- Related:
- Docs updated:
- Spawn trace:

<!-- Release label: set one `release:*` label. The bump tracks breakage, not
     size (`docs/releasing.md`): `release:patch` / `release:stable` breaks
     nothing; `release:minor` / `release:major` break existing behaviour,
     config or state; `release:rc` cuts a prerelease; `release:skip` releases
     nothing. No label, or an unknown one, defaults to RC. -->
