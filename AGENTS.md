# meridian-cli

Multi-agent coordination CLI. Launches harness subprocesses, tracks state
on disk, surfaces results. Coordinates agents — does not control them.

No real users, no backwards compatibility. Change the schema to get it right.

## Safety Rules

NEVER REVERT CHANGES — always assume it's someone else's work.

**NEVER merge a PR into `main` without explicit human approval in the current
conversation.** Stop at draft→ready with a `release:*` label. Approval to merge
one PR does not carry to the next. Merging lanes into a task branch is fine —
the gate is `main`.

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
uv run pytest-llm                              # Test (token-efficient)
uv run --extra dev python -m pyright           # Type check (0 errors required)
uv add <package>                               # Add dependency
```

Use `rtk` for noisy commands (`rtk git diff`, `rtk pytest`, `rtk rg "<pattern>"`).

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

Read `docs/releasing.md` before releasing or opening a PR.

PRs: use `.github/PULL_REQUEST_TEMPLATE.md` — fill every section. Set a
`release:*` label before merging. No label defaults to RC.

Prompt packages: `meridian mars version patch [--push]` from the prompt repo.
meridian-cli and mars-agents: CI auto-release on labeled PR merge.

## Testing

Prefer smoke tests over unit tests. See `tests/AGENTS.md`.

## Sibling Repos

Read the target repo's AGENTS.md before operating on it.

- `../prompts/meridian-base`, `../prompts/meridian-dev-workflow` — prompt packages
- `../mars-agents` — package manager (Rust)

## Downlinks

- `src/meridian/AGENTS.md` — layered architecture, subpackage index
- `tests/AGENTS.md` — test placement, fakes
- `docs/releasing.md` — complete release workflow
- `.context/CONTEXT.md` — cross-cutting design tensions
