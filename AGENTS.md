# meridian-cli

Multi-agent coordination CLI. Launches harness subprocesses, tracks state
on disk, surfaces results. Coordinates agents — does not control them.

Compatibility: one-minor-release deprecation for config, CLI, hook names, and JSON output
fields; new builds read state older builds wrote (`docs/releasing.md#compatibility-and-versioning`).

## Safety Rules

Other agents and Jimmy work in this checkout at the same time. Leave changes you didn't
make, even ones that look wrong: report them instead. Change branches only in a worktree
(`git worktree add ../meridian-cli.worktrees/<name> -b <branch> <base>`, then `--task-dir
<worktree>` for spawns); the primary checkout stays on `main`.

Merging a PR into `main` takes Jimmy's approval in the current conversation, one PR at a
time, unless your global instructions grant your model more; without it, stop at a ready
PR with a `release:*` label. A change that doesn't affect what Meridian does when it runs
(docs, comments, `AGENTS.md`) goes straight to `main` with no PR. Merging lanes into a task
branch, or reviewed part PRs into a `release/v<X>.<Y>.0` branch, needs no approval.

A fix restores intended behavior; changing what users are meant to see is a feature. A big
refactor, reorganization or architecture change needs Jimmy's approval first, quoted in
its issue or PR; another agent's report of that approval doesn't count. Before a merge, CI
is green, the regressions the change could cause are exercised on the real system (`/testing`), and the PR body is reread
against the final diff, with evidence from the commit being merged. A big fix or refactor
also needs an independent review. A merge can release, so take the label with care:
`release:patch` publishes to users, and a change with no user effect takes `release:skip`.

Big means impact, not size: a change is big when it crosses a boundary others depend on.
That is the CLI's commands, flags or output, a config or profile format, files under `.meridian/` or `~/.meridian/`, the harness adapter seam, or the Mars package contract. A change that looks narrow but alters behavior others rely on is big
too; when unsure, call it big.

CI creates `v*` tags, and a pushed tag publishes a release: never push one yourself. The
pre-push hook runs the fast gate; fix what it catches rather than skipping it with
`--no-verify`.

`meridian mars sync` rewrites the generated directories (`.claude/agents/`,
`.claude/skills/`, `.cursor/`, `.codex/`, `.opencode/`, `.pi/`); edit the source packages
in their sibling checkouts.

## Invariants

These hold however the work is done, including through the Rust rewrite. Change one only as its own visible step, never to get a task passing. Everything else in this file is a current choice, open to rewriting when evidence shows something better.

| Invariant | Check |
|---|---|
| An upgrade never loses or silently misreads user state | Partial: legacy-row tests |
| A crash never loses an acknowledged write or accepts an incomplete one | `tests/platform/test_atomic.py`, JSONL tail-repair tests |
| Meridian never acts beyond what was authorized: it doesn't signal unverified processes, start cancelled work or widen a workspace | Scattered by area |
| Private content never reaches the wrong recipient: secrets, another session's transcript, turn text that was excluded | Scattered by area |
| A lost restriction that changes what an agent can do is shown by default. Every other lost field is available under `--verbose`. Nothing is dropped without a record. | None yet |
| Meridian is an overlay on the harness. A session it starts stays a normal native session: the conversation lives in the harness's own store and format, and can be resumed with the harness alone. Removing Meridian leaves the harness and its sessions working. | `tests/smoke/test_overlay_resume.py` (opt-in, paid) |

A restriction is lost when a harness can't express part of a profile. It shows in Mars's default output and in `--dry-run`, not at launch.

## Comments

Few. Write one only for what the code can't show and a wrong edit would break silently, at the line it guards. No decision ids.

## Dev Commands

`uv` exclusively — never `pip` or raw `python`.

```bash
uv sync --extra dev                            # Install
uv run ruff check .                            # Lint
uv run ruff format .                           # Format (the gate runs --check)
uv run python -m meridian.dev.lints             # Source-tree rules (also a preflight step)
uv run pytest-llm                              # Test (token-efficient; default paths, stops at first failure)
uv run pytest tests/                           # Full suite: run before calling a branch green
uv run --extra dev python -m pyright --warnings # Type check (0 errors, 0 warnings)
uv add <package>                               # Add dependency
```

## Design Constraints

- **Harness-agnostic**: adapters bridge to Claude/Codex/OpenCode. Harness specifics stay in `lib/harness/`.
- **Files as authority**: authoritative state is stored under `~/.meridian/`;
  a project's repo-local `.meridian/` is never created. Rebuildable SQLite projections may index
  authoritative files, but must never become the only copy of state.
- **Crash-only**: atomic writes (tmp+rename), truncation-tolerant reads. Recovery IS startup.
- **Extend through seams**: new harness = one adapter + registration. New command = one module. 10-file edits = wrong abstraction.
- **POSIX-first**: Linux/macOS are the supported platforms. Native Windows was never made to work and is not planned — design for the simplest correct POSIX behavior; don't add Windows-specific machinery (existing `os.name` branches may stay, untested). Still use `get_user_home()`/`get_home_path()`, never hardcode paths.
- **No VCS dependency**: core ops work without git.

Config precedence: CLI flags > ENV vars > YAML profile > Project config > User config > harness default.
Derived fields inherit the precedence level of their source.

## Release & PRs

Read `docs/releasing.md` before releasing or opening a PR. The pre-push hook
runs the fast gate: push after the last review fix. Put changelog entries in
the PR body's `## Changelog`, not `CHANGELOG.md`; use the PR template and
select a `release:*` label. Only a change that doesn't affect execution skips
the PR.

## Testing

Prefer manual smoke tests over unit tests. See `tests/AGENTS.md`.
When manually smoke testing and probing, make sure the models you use are cheap

## Sibling Repos

Read the target repo's AGENTS.md before operating on it.

- `../prompts/meridian-base`, `../prompts/meridian-dev-workflow` — prompt packages
- `../mars-agents` — package manager (Rust)

Make prompt changes (agents, skills) at their source in `../prompts/<package>`, on `main`
in that checkout. Don't create branches or worktrees for them unless the user asks. Stage
the change and leave it uncommitted; commit and push only once the user approves. The
shared rules for every package are in `../prompts/AGENTS.md`.

## Downlinks

- `src/meridian/AGENTS.md` — plugin surface, launch entry points, typing rules
- `tests/AGENTS.md` — test placement, fakes
- `docs/releasing.md` — complete release workflow
- `.context/CONTEXT.md` — cross-cutting design tensions
