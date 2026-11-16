# meridian-cli

Meridian lets agents in Claude Code, Codex, OpenCode, Cursor and Pi spawn each other.
It launches, tracks and reports; orchestration belongs in agent prompts, not here.

## Who It's For

- **The person running agents.** They use several harnesses, kill and restart things,
  and come back to sessions days later. They must be able to drop Meridian at any time
  and keep working in their harnesses.
- **The agent calling `meridian`.** It can't ask what a flag means: it reads `-h` and
  the output, and scripts parse the JSON.

## Engineering

This codebase is yours: how it's built is your call, and you'll keep building on it.
Product decisions are Jimmy's.

**Choose the simpler system, not the smaller change.** When a case doesn't fit, change the
design so it does instead of adding a branch, flag or special case. Judge a choice by the
code it leaves: a bigger change, or more deletion, is the right one when less is left to
understand. Fix structural debt in the same PR, whatever its size; a found fix too large for
the PR gets its own PR now.

**Fix the root cause.** Gather the problems a probe, a review or Jimmy finds, trace each to
its cause, and fix the cause: no guard, retry or special case where the problem showed up.
Several problems often share one cause. A rare edge case with no fix at its cause can be left
as it is.

## Design Constraints

- A session Meridian starts stays a native session in the harness's own store, resumable
  without Meridian: the user can drop Meridian at any time and lose nothing.
- Meridian gets killed mid-write all the time: every write is atomic, every read survives
  a half-written file, and recovery runs at startup.
- A new build reads what older builds wrote. A flag, config key, hook name or JSON field
  keeps working for one minor release after it's deprecated.
- Meridian signals only processes it verified as its own, never starts cancelled work and
  never widens a workspace.
- Secrets, a session's transcript and turn text the user excluded never reach a session
  that wasn't meant to see them.
- Harness specifics stay in `lib/harness/`. A new harness is one adapter plus
  registration and a new command is one module; a change that has to touch ten files
  means the abstraction is wrong, so fix the abstraction.
- State lives under `~/.meridian/` as files; SQLite may index them but is never the only
  copy. A project's repo-local `.meridian/` is never created.
- Linux and macOS are supported; Windows isn't planned. Existing Windows branches may
  stay, untested; add no new ones. Resolve home with `get_user_home()`.
- Core operations work without git.
- Comments only for what the code can't show and a wrong edit would break silently;
  no decision IDs.

How the code keeps the first five today, what checks them and where it falls short:
[.context/design-constraints.md](.context/design-constraints.md).

## Commands

Use `uv`, never `pip` or raw `python`.

```bash
uv sync --extra dev                              # install
uv run pytest-llm                                # tests, stops at first failure
uv run pytest tests/                             # full suite
uv run ruff check . && uv run ruff format .      # lint, format
uv run --extra dev python -m pyright --warnings  # types: 0 errors, 0 warnings
uv run python -m meridian.dev.lints              # source-tree rules
```

## Shared Checkout

Other agents and Jimmy work here at the same time. Leave changes you didn't make, even
ones that look wrong, and report them. Change branches only in a worktree
(`../meridian-cli.worktrees/<name>`, passed to spawns as `--task-dir`).

Each area's `AGENTS.md` holds the rules that guard it; read it before changing that
area. `meridian mars sync` rewrites `.claude/`, `.codex/`, `.cursor/`, `.opencode/` and
`.pi/`: change agents and skills at their source in `../prompts/<package>`
(`../prompts/AGENTS.md`).

## Merging and Releasing

A fix restores intended behavior; changing what users are meant to see is a feature.
A change that doesn't affect what Meridian does when it runs (docs, comments,
`AGENTS.md`) goes straight to `main`. Everything else goes through a PR, and merging
one into `main` takes Jimmy's approval in this conversation, per PR, unless your global
instructions grant your model more. A change that crosses a boundary others depend on
(CLI commands, flags or output, a config or profile format, files under `~/.meridian/`,
the harness adapter seam, the Mars package contract) needs his approval before it
starts. Before opening a PR or merging, read [docs/releasing.md](docs/releasing.md).
