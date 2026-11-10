# meridian-cli

Meridian lets agents in Claude Code, Codex, OpenCode, Cursor and Pi spawn each other.
It launches, tracks and reports; orchestration belongs in agent prompts, not here.

## Who It's For

- **The person running agents.** Meridian is an overlay for them: a session it starts
  stays a normal native session, resumable with the harness alone, and removing
  Meridian leaves their harnesses working. A crash never loses what Meridian
  acknowledged, and an upgrade never loses or misreads their state. Meridian never
  acts beyond what they authorized, and never hands one session's private content
  (secrets, transcripts, excluded turn text) to another.
- **The agent calling `meridian`.** It can't ask what a flag means; it reads `-h` and
  the output. A flag, config key, hook name or JSON field keeps working for one minor
  release after it's deprecated.

These promises are the project's invariants: they hold however the work is done, and
one changes only as its own step, agreed with Jimmy.

## Design Constraints

- Harness specifics stay in `lib/harness/`. A new harness is one adapter plus
  registration and a new command is one module; a change that has to touch ten files
  means the abstraction is wrong, so fix the abstraction.
- State lives under `~/.meridian/` as files; SQLite may index them but is never the only
  copy. A project's repo-local `.meridian/` is never created.
- Crash-only, which keeps the crash promise: harnesses exit, users press Ctrl-C and
  laptops sleep mid-write, so every write is atomic (tmp+rename), every read tolerates
  a truncated file, and recovery happens at startup rather than in a cleanup path that
  may never run. `lib/state/AGENTS.md` has the lock order and tail repair.
- Linux and macOS only; add no Windows-specific code. Resolve home with
  `get_user_home()`.
- Core operations work without git.
- Comments only for what the code can't show and a wrong edit would break silently;
  no decision IDs.

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
