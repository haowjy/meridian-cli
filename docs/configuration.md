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
| `harness.pi.load_all_pi_extensions` | bool | Retain ambient extensions in spawned RPC and scan `extra_extension_paths` in both roles (default `false`; primary always retains native discovery) |
| `harness.pi.extra_extension_paths` | array[str] | Extra extension roots scanned only when `load_all_pi_extensions = true` (default: Pi user extension dir) |
| `harness.pi.background_tasks.enabled` | bool | Toggles `managed-bash` in primary and spawned sessions (`bash` / `bash_manage`, `/ps*`; default `true`) |
| `harness.pi.spawn_watch.enabled` | bool | Toggles the `meridian-spawn-watch` extension (`/spawn` spawn discovery + wait; default `true`) |
| `harness.pi.disable_managed_bash` | bool | **Legacy** — same as `background_tasks.enabled = false` |
| `output.show` | array[str] | Stream categories shown |
| `output.verbosity` | str\|null | `quiet\|normal\|verbose\|debug` |
| `state.retention_days` | int | TTL for stale state pruning (`-1` = never, `0` = immediate, default `30`) |
| `spawn.default_wait_yield_seconds` | float | Default yield interval for `spawn wait` (seconds) |
| `spawn.min_wait_yield_seconds` | float | Minimum yield interval for `spawn wait` (seconds) |
| `primary.autocompact` | int | Context-token compaction threshold for the primary session (minimum 1000) |
| `primary.autocompact_pct` | int | Context-window percentage for primary-session compaction (1–100) |

Pi's `session-boundary` extension is always loaded in both roles; it has no config
toggle. It records launch-correlated native lifecycle evidence for post-exit session
verification. See the [Pi extension guide](../src/meridian/pi_runtime/README.md).

## Notifications and idle sessions

`meridian notify` sends a one-shot message through the selected push and email
backends. The idle service uses the same delivery settings for its push, cache
warning, and compaction-result messages.

### `[notify]`

| Key | Default | Environment variable | Meaning |
|---|---|---|---|
| `push_backend` | `"ntfy"` | `MERIDIAN_NOTIFY_PUSH_BACKEND` | `ntfy`, `command`, or `none` |
| `email_backend` | `"gmail"` | `MERIDIAN_NOTIFY_EMAIL_BACKEND` | `gmail`, `smtp`, `ntfy`, `command`, or `none` |
| `include_messages` | `true` | `MERIDIAN_NOTIFY_INCLUDE_MESSAGES` | Include the last user and assistant excerpts in idle push and warning notifications |
| `ntfy_server` | `"https://ntfy.sh"` | `MERIDIAN_NOTIFY_NTFY_SERVER` | Base URL for the ntfy server |
| `ntfy_topic` | unset | `MERIDIAN_NOTIFY_NTFY_TOPIC` | ntfy topic; an unset topic disables ntfy delivery with a warning |
| `email_to` | unset | `MERIDIAN_NOTIFY_EMAIL_TO` | Email recipient |
| `email_from` | unset (uses `smtp_user`) | `MERIDIAN_NOTIFY_EMAIL_FROM` | Email sender |
| `smtp_user` | unset | `MERIDIAN_NOTIFY_SMTP_USER` | SMTP login; also the default sender |
| `smtp_host` | `"smtp.gmail.com"` | `MERIDIAN_NOTIFY_SMTP_HOST` | SMTP STARTTLS host; the `gmail` backend always uses Gmail's host |
| `smtp_port` | `587` | `MERIDIAN_NOTIFY_SMTP_PORT` | SMTP STARTTLS port; the `gmail` backend always uses port 587 |
| `smtp_password_file` | unset | `MERIDIAN_NOTIFY_SMTP_PASSWORD_FILE` | File containing the SMTP password |
| `push_command` | unset | `MERIDIAN_NOTIFY_PUSH_COMMAND` | Command used by the push `command` backend |
| `email_command` | unset | `MERIDIAN_NOTIFY_EMAIL_COMMAND` | Command used by the email `command` backend |

The command backends split their configured command with `shlex`, append the
notice title and body as arguments, and pass the complete notice as JSON on
stdin. Delivery is attempted once per selected backend; one backend failing does
not prevent the other from running.

Notification headlines identify the agent and work item (or project). Bodies put
each fact on its own line: message excerpts are labelled `You` and `Assistant`,
and `tmux: <session>` appears last when the process is inside tmux. Excerpts are
plain text, whitespace-collapsed, and shortened before storage. Because they leave
the machine through ntfy or email, set `include_messages = false` to omit both.
For `meridian notify`, `--title` replaces the headline and moves the agent/work
description into the body so the context remains visible.

Put SMTP credentials in `smtp_password_file` and set its mode to `0600`.
Meridian refuses a password file readable by the group or other users. The
fallback `MERIDIAN_NOTIFY_SMTP_PASSWORD` is read only when no password file is
configured. It is deliberately not a TOML key: every spawned agent inherits the
variable and can write it into a transcript, so use it only when that exposure
is acceptable.

For the `gmail` backend the password must be a Google app password, not the
account password: turn on 2-Step Verification for the account, create an app
password at <https://myaccount.google.com/apppasswords>, and store it in
`smtp_password_file`. `smtp_user` is the Gmail address that owns the app
password and is the default sender; `email_to` can be any address. Check the
setup with `meridian notify "test" --json`: `"channel": "gmail", "status":
"sent"` means it went out, and a `535` error means the app password is wrong
or 2-Step Verification is off.

### `[idle]`

| Key | Default | Environment variable | Meaning |
|---|---:|---|---|
| `enabled` | `true` | `MERIDIAN_IDLE_ENABLED` | Enable automatic idle handling |
| `push_seconds` | `60` | `MERIDIAN_IDLE_PUSH_SECONDS` | Seconds after a long turn before the waiting push |
| `long_turn_seconds` | `120` | `MERIDIAN_IDLE_LONG_TURN_SECONDS` | Minimum turn length that uses `push_seconds` |
| `quick_turn_push_seconds` | `600` | `MERIDIAN_IDLE_QUICK_TURN_PUSH_SECONDS` | Seconds after a quick or unknown-length turn before the waiting push |
| `warn_minutes` | `15` | `MERIDIAN_IDLE_WARN_MINUTES` | Minutes before cache expiry to warn |
| `warn_email` | `true` | `MERIDIAN_IDLE_WARN_EMAIL` | Include the configured email backend in the warning |
| `compact_minutes` | `5` | `MERIDIAN_IDLE_COMPACT_MINUTES` | Minutes before cache expiry to try compaction |
| `compact` | `true` | `MERIDIAN_IDLE_COMPACT` | Permit idle compaction |
| `min_compact_tokens` | `40000` | `MERIDIAN_IDLE_MIN_COMPACT_TOKENS` | Skip compaction when a known context size is smaller |
| `late_fire_tolerance_seconds` | `120` | `MERIDIAN_IDLE_LATE_FIRE_TOLERANCE_SECONDS` | Skip a timer that fires more than this many seconds late |

After a turn lasting at least two minutes, the waiting push follows 60 seconds
later; after a quick or unknown-length turn, it waits 10 minutes. Set
`long_turn_seconds = 0` to restore a push after every turn using `push_seconds`.
Codex observes the user return only when that turn ends, so its turn length is
unknown and uses the 10-minute delay. The warning and compaction are scheduled
relative to the cache TTL; a push is omitted when it would not land strictly
before a valid warning, and other stages are omitted when they do not fit
strictly after their predecessor. An unknown TTL therefore produces only the
push.

### `[harness.<harness>.idle]`

Use `claude`, `codex`, `opencode`, or `pi` for `<harness>`.

| Key | Default | Environment variable |
|---|---|---|
| `enabled` | `true` | `MERIDIAN_HARNESS_IDLE_ENABLED_<H>` |
| `compact` | `true` | `MERIDIAN_HARNESS_IDLE_COMPACT_<H>` |
| `ttl_seconds` | Claude/Pi: sensed; Codex: `1800`; OpenCode: `300` | `MERIDIAN_HARNESS_IDLE_TTL_SECONDS_<H>` |

`<H>` is the uppercase harness name, for example
`MERIDIAN_HARNESS_IDLE_TTL_SECONDS_CODEX`.

Idle settings resolve **precedence level first, then specificity**:

```text
per-harness environment > global environment
  > per-harness file key > global file key
  > harness default
```

Within the file level, later layers win: user config, then project config, then
local config. Environment always beats every file, so a per-tmux
`MERIDIAN_IDLE_COMPACT=0` or `MERIDIAN_IDLE_COMPACT=1` always wins, even over a
`[harness.<harness>.idle] compact` file setting.

### Default behavior and compaction guards

| Harness | Default behavior |
|---|---|
| Claude | Push, warning, and compaction; the mod reads the cache TTL from the transcript |
| Pi | Push, warning, and compaction: Meridian defaults `PI_CACHE_RETENTION=long` for interactive primaries when it is absent; `openai` is scheduled against 30 minutes, because OpenAI's `24h` retention typically lasts about that long. Set `PI_CACHE_RETENTION=short` to keep Pi's short-cache default. Unknown provider IDs remain push-only. |
| Codex | Push, warning, and compaction with a conservative 1800-second TTL |
| OpenCode | Push only with the 300-second default because the warning and compaction do not fit; raise `ttl_seconds` to schedule them |

The Pi cache-retention default is launch behavior, independent of whether
`[idle]` is enabled: an interactive Pi session gets the longer provider cache
even when idle notifications and compaction are off.

Idle automation runs only in an interactive primary session. Each stage is
claimed at most once between user prompts. A compaction is skipped when the
timer is stale or late, idle or compaction is disabled, the cache is already
cold, the harness is busy, a draft is present or cannot be determined, an
agent or child spawn is still running, the known context is below
`min_compact_tokens`, or the harness's own automatic compaction is disabled.
An unknown context size is allowed; an unknown draft is not. A positively
identified user prompt closes the idle stretch immediately.

### Opt in an external Claude TUI

Meridian injects its bundled Claude mod automatically into interactive Claude
primaries it launches. To enable the same behavior in a Claude TUI started
outside Meridian:

```bash
CLAUDE_CODE_PLUGIN_DIRS="$(meridian idle mod-path)" claude
```

`MERIDIAN_SESSION_ROLE` is unset in that case. The mod opts in only when Claude
reports an interactive TUI surface; print mode (`claude -p`) remains inert.
The usual `[notify]` and `[idle]` config and environment overrides still apply.

Agent profiles are opt-in. When `--agent/-a` is omitted and `primary.agent` is unset,
Meridian runs without a predefined profile. Pass `-a ""` to explicitly clear
`primary.agent` for one launch. Agent definitions live in `mars.toml`, not
`meridian.toml`; run `meridian mars sync` after changing them.

Project-level routing defaults (`default_model`, `default_harness`) live in
`mars.toml` under `[settings]`, not in Meridian config.

## History ZIP retention

`[history.archive]` controls optional history retention, not UI visibility archive
or stale-state pruning. Automatic retention is **off** by default. Manual and
automatic passes share the same protection, verification and reclaim policy.

| Key | Type | Default / purpose |
|---|---|---|
| `history.archive.automatic` | bool | `false`; enable finite retention passes after primary sessions stop |
| `history.archive.after_days` | int | `30`; days since last activity, not creation |
| `history.archive.interval_hours` | int | `24`; minimum interval between automatic passes |
| `history.archive.max_records` | int | `256`; maximum selected records per pass |
| `history.archive.max_uncompressed_bytes` | int | `1073741824` (1 GiB); bundle target, allowing one oversized record alone |
| `history.archive.destination` | str\|null | Unset; local/mounted ZIP destination, required for archiving |

Use `meridian config set/get/reset` with these canonical keys. The archive
command's `--destination` and `--after-days` override configured values. The
supported environment overrides are:

| Variable | Config key |
|---|---|
| `MERIDIAN_HISTORY_ARCHIVE_AUTOMATIC` | `history.archive.automatic` |
| `MERIDIAN_HISTORY_ARCHIVE_AFTER_DAYS` | `history.archive.after_days` |
| `MERIDIAN_HISTORY_ARCHIVE_DESTINATION` | `history.archive.destination` |

ZIPs are retained indefinitely; active/dependent or changed records remain loose.
See [History storage and retention](history.md) for a TOML example, filesystem
requirements, dry-run/apply behavior and selective inert restore.

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
