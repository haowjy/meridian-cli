# meridian-cli

Meridian lets agents in Claude Code, Codex, OpenCode, Cursor and Pi spawn each other.
It launches, tracks and reports; orchestration belongs in agent prompts, not here.

## Who It's For

- **The person running agents.** They use several harnesses, kill and restart things,
  and come back to sessions days later. They must be able to drop Meridian at any time
  and keep working in their harnesses.
- **The agent calling `meridian`.** It can't ask what a flag means: it reads `-h` and
  the output, and scripts parse the JSON.

## Invariants

These hold however the code changes. Change one only as its own step, agreed with Jimmy.

- **Overlay.** A session Meridian starts stays a native session in the harness's own
  store, resumable without Meridian, so removing Meridian loses the user nothing.
- **Crash-only.** Every write is atomic, every read survives a half-written file, and
  recovery runs at startup. Meridian gets killed mid-write all the time; whatever it
  reported as saved must still be there afterwards.
- **Compatible upgrades.** A new build reads what older builds wrote, and a flag,
  config key, hook name or JSON field keeps working for one minor release after it's
  deprecated, so an upgrade breaks neither the user's history nor their scripts.
- **Only what was authorized.** Meridian signals only processes it verified as its own,
  never starts cancelled work and never widens a workspace: it runs beside the user's
  other sessions.
- **Private content stays put.** Secrets, a session's transcript and turn text the user
  excluded never reach a session that wasn't meant to see them.

## Design Constraints

- Harness specifics stay in `lib/harness/`. A new harness is one adapter plus
  registration and a new command is one module; a change that has to touch ten files
  means the abstraction is wrong, so fix the abstraction.
- State lives under `~/.meridian/` as files; SQLite may index them but is never the only
  copy. A project's repo-local `.meridian/` is never created.
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
