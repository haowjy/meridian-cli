# meridian-cli

Multi-agent coordination CLI. Launches harness subprocesses, tracks state
on disk, surfaces results. Coordinates agents — does not control them.

Compatibility: one-release deprecation for config, CLI, and hook names; new builds
read state older builds wrote (`docs/releasing.md#compatibility-and-versioning`).

## Safety Rules

NEVER REVERT CHANGES — always assume it's someone else's work.

**NEVER merge a PR into `main` without explicit human approval in the current
conversation.** Stop at draft→ready with a `release:*` label. Approval to merge
one PR does not carry to the next. Two exceptions go straight to `main` without
approval: docs-only changes, and tiny, obvious bug fixes. Merging lanes into a task
branch, or your reviewed part PRs into a `release/v<X>.<Y>.0` branch, needs no approval.

**NEVER use `--no-verify` on git push** unless explicitly instructed.

**NEVER manually create or push `v*` tags.** Tags are CI-owned.

**NEVER edit generated target directories** (`.claude/agents/`, `.claude/skills/`,
`.cursor/`, `.codex/`, `.opencode/`, `.pi/`). Overwritten by `meridian mars sync`.
Edit source package repos in sibling checkouts.

**Never switch the branch of a checkout you don't own** — it may be shared. Need
another branch? Make a worktree (`git worktree add ../meridian-cli.worktrees/<name>
-b <branch> <base>`) and pass `--task-dir <worktree>` to spawns.

## Dev Commands

`uv` exclusively — never `pip` or raw `python`.

```bash
uv sync --extra dev                            # Install
uv run ruff check .                            # Lint
uv run pytest-llm                              # Test (token-efficient; default paths, stops at first failure)
uv run pytest tests/                           # Full suite: run before calling a branch green
uv run --extra dev python -m pyright --warnings # Type check (0 errors, 0 warnings)
uv add <package>                               # Add dependency
```

## Design Constraints

- **Harness-agnostic**: adapters bridge to Claude/Codex/OpenCode. Harness specifics stay in `lib/harness/`.
- **Files as authority**: authoritative state is stored under `~/.meridian/`
  (user) or `.meridian/` (project). Rebuildable SQLite projections may index
  authoritative files, but must never become the only copy of state.
- **Crash-only**: atomic writes (tmp+rename), truncation-tolerant reads. Recovery IS startup.
- **Extend through seams**: new harness = one adapter + registration. New command = one module. 10-file edits = wrong abstraction.
- **POSIX-first**: Linux/macOS are the supported platforms. Native Windows was never made to work and is not planned — design for the simplest correct POSIX behavior; don't add Windows-specific machinery (existing `os.name` branches may stay, untested). Still use `get_user_home()`/`get_home_path()`, never hardcode paths.
- **No VCS dependency**: core ops work without git.

Config precedence: CLI flags > ENV vars > YAML profile > Project config > User config > harness default.
Derived fields inherit the precedence level of their source.

## Release & PRs

Read `docs/releasing.md` before releasing or opening a PR. The pre-push hook runs
the fast gate: push after the last review fix.

Docs never state release status ("unreleased", "next release"): the release
commit stamps the version into `CHANGELOG.md`, and `[Unreleased]` is only an
empty anchor. Changelog entries go in the PR body's `## Changelog` section; the
release job renders them, so never edit `CHANGELOG.md` in a PR. Title upgrade
notes in `docs/upgrading.md` by topic; the changelog entry that links to them
dates them.

Docs-only changes (`docs/`, `AGENTS.md`, `.context/`) and tiny, obvious bug fixes need no
PR: push them straight to `main`. A direct push to `main` releases nothing.

PRs: use `.github/PULL_REQUEST_TEMPLATE.md` — fill every section. Set a
`release:*` label before merging; no label defaults to RC.

Minor and major changes never merge to `main` piece by piece: each one goes to the open
`release/v<X>.<Y>.0` branch (create it if none is open), and that branch's single PR to
`main` carries the
`release:minor`/`release:major` label
(`docs/releasing.md#minor-and-major-releases-collect-on-a-release-branch`).

Prompt packages: `meridian mars version patch [--push]` from the prompt repo.
meridian-cli and mars-agents: CI auto-release on labeled PR merge.

## Testing

Prefer manual smoke tests over unit tests. See `tests/AGENTS.md`.
When manually smoke testing and probing, make sure the models you use are cheap

## Sibling Repos

Read the target repo's AGENTS.md before operating on it.

- `../prompts/meridian-base`, `../prompts/meridian-dev-workflow` — prompt packages
- `../mars-agents` — package manager (Rust)

## Downlinks

- `src/meridian/AGENTS.md` — layered architecture, subpackage index
- `tests/AGENTS.md` — test placement, fakes
- `docs/releasing.md` — complete release workflow
- `.context/CONTEXT.md` — cross-cutting design tensions
