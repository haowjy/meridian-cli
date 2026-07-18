# Configuration

Meridian now has two configuration surfaces:

- **Mars config** (`mars.toml`, plus local Mars overlays) owns package content:
  dependencies, materialization targets, model aliases/catalog settings, project
  routing defaults, and per-agent runtime overlays.
- **Meridian config** (`~/.meridian/config.toml`, `meridian.toml`,
  `meridian.local.toml`) owns Meridian CLI/runtime behavior: timeouts, output,
  state retention, work/context/workspace roots, hooks, harness defaults, and
  primary-session defaults.

Meridian reads agents, skills, and model aliases from the repo-local `.mars/`
compiled store. Harness-specific directories such as `.claude/`, `.codex/`,
`.opencode/`, `.pi/`, and `.cursor/` are Mars targets, not Meridian discovery
roots. Run `meridian mars sync` to populate `.mars/` and linked targets from
configured package sources.

## Quick Start

```bash
meridian
meridian config show
meridian config init
meridian config set defaults.max_retries 5
meridian config get defaults.max_retries
meridian config reset defaults.max_retries
meridian mars models list
```

Put routing/package changes in `mars.toml`:

```toml
[settings]
targets = [".claude", ".codex", ".opencode"]
default_model = "gptmini"
default_harness = "codex"

[models.gptmini]
provider = "openai"
model = "gpt-5.4-mini"
harness = "codex"

[agents.reviewer]
model = "gptmini"
effort = "medium"
approval = "auto"
```

## Repository Layout

`meridian.toml` is the committed project anchor. Its machine-managed identity is precedence-exempt:

```toml
# managed by meridian — do not edit
[project]
id = "calm-river-stone"
```

The ID keys all mutable state outside the repository:

```text
~/.meridian/projects/<id>/    # runtime, locks, caches, autosync metadata
~/.meridian/context/<id>/     # default work, archive, and KB roots
```

A directory with `meridian.toml` or `mars.toml` is a Meridian project. Read-only commands also run in directories with neither file and create nothing. The first durable write creates `meridian.toml` and `[project] id`; a `mars.toml`-only directory is handled the same way. Identity is committed and immutable, so clones and worktrees share runtime history. No state or generated `.gitignore` lives in a repo-local `.meridian/` directory.

## `meridian.toml` Keys

Use `meridian.toml` for Meridian runtime behavior only. Do **not** put
`[agents.<name>]`, `default_model`, or `default_harness` here; those belong in
Mars config.

Canonical keys accepted by `meridian config set/get/reset`:

| Key | Type | Purpose |
|---|---|---|
| `defaults.max_depth` | int | Max zero-based delegated spawn depth |
| `defaults.max_retries` | int | Retry attempts per run |
| `defaults.retry_backoff_seconds` | float | Retry backoff multiplier |
| `timeouts.kill_grace_minutes` | float | Grace before force-kill (minutes) |
| `timeouts.guardrail_minutes` | float | Guardrail timeout (minutes) |
| `timeouts.startup_minutes` | float | Startup-phase timeout for backend boot, connection, and session handshake (minutes) |
| `timeouts.wait_minutes` | float | Default `spawn wait` timeout (minutes) |
| `timeouts.pi_child_wave_timeout_seconds` | float | Pi spawn-watch tracked-child wave timeout (seconds; default 300 when unset) |
| `timeouts.resident_rearm_budget` | int | Maximum resident deadline extensions (nonnegative; unlimited when unset) |
| `timeouts.pi_task_ping_interval_seconds` | float | Pi background-task ping interval (seconds; extension default when unset) |
| `harness.claude` | str | Default model for Claude harness |
| `harness.codex` | str | Default model for Codex harness |
| `harness.opencode` | str | Default model for OpenCode harness |
| `harness.pi.load_all_pi_extensions` | bool | When `true`, Pi also loads extensions from `extra_extension_paths` (default `false` = Meridian bundles only) |
| `harness.pi.extra_extension_paths` | array[str] | Extra extension roots scanned only when `load_all_pi_extensions = true` (default: Pi user extension dir) |
| `harness.pi.background_tasks.enabled` | bool | Toggles the `managed-bash` extension (`/ps` background bash tasks; default `true`) |
| `harness.pi.spawn_watch.enabled` | bool | Toggles the `meridian-spawn-watch` extension (`/spawn` spawn discovery + wait; default `true`) |
| `harness.pi.disable_managed_bash` | bool | **Legacy** — same as `background_tasks.enabled = false` |
| `output.show` | array[str] | Stream categories shown |
| `output.verbosity` | str\|null | `quiet\|normal\|verbose\|debug` |
| `state.retention_days` | int | TTL for stale state pruning (`-1` = never, `0` = immediate, default `30`) |
| `spawn.default_wait_yield_seconds` | float | Default yield interval for `spawn wait` (seconds) |
| `spawn.min_wait_yield_seconds` | float | Minimum yield interval for `spawn wait` (seconds) |
| `primary.autocompact` | int | Context compaction threshold for primary session (1–100) |

Agent profiles are opt-in. When `--agent/-a` is omitted and `primary.agent` is unset,
Meridian runs without a predefined profile. Pass `-a ""` to explicitly clear
`primary.agent` for one launch. Agent definitions live in `mars.toml`, not
`meridian.toml`; run `meridian mars sync` after changing them.

Project-level routing defaults (`default_model`, `default_harness`) live in
`mars.toml` under `[settings]`, not in Meridian config.

## Config Precedence

For config-file resolution, Meridian layers sources in this order:

1. `~/.meridian/config.toml` (lowest)
2. `meridian.toml`
3. `meridian.local.toml` (highest file precedence)

Environment variables still override all file values.

## Mars-Owned Routing and Agent Runtime

Mars owns package materialization, project routing defaults, model aliases, and
per-agent runtime policy. See [Mars configuration and agent runtime](configuration/mars.md).

## Example

```toml
[defaults]
max_depth = 4

[harness]
claude = "claude-opus-4-6"
codex = "gpt-5.3-codex"
opencode = "gemini-3.1-pro"

[harness.pi]
load_all_pi_extensions = false
background_tasks.enabled = true
spawn_watch.enabled = true
# disable_managed_bash = false  # legacy alias for background_tasks.enabled

[output]
show = ["lifecycle", "error"]
verbosity = "verbose"

[primary]
autocompact = 70

[state]
retention_days = 30   # -1 = never prune, 0 = prune immediately
```

## Workspace

Workspace config defines filesystem roots projected into harness launches. See
[workspace configuration](configuration/workspace.md).

## Hooks

Hooks are configured in any Meridian config file. See [hooks.md](hooks.md) for
schema, event names, builtins, and shell behavior guidance.

## Context

Context config locates active work, knowledge bases, and work archives. See
[context configuration](configuration/context.md).

## Model Catalog

See [model catalog configuration](configuration/model-catalog.md).

## Cursor Harness

See [Cursor harness configuration](configuration/cursor.md).

## Environment Variables

See [environment variables](configuration/environment.md).
