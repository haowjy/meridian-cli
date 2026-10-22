# Configuration

Meridian now has two configuration surfaces:

- **Mars config** (`mars.toml`, plus local Mars overlays) owns package content:
  dependencies, materialization targets, model aliases/catalog settings, project
  routing defaults, and per-agent runtime overlays.
- **Meridian config** (`~/.meridian/config.toml`, `meridian.toml`,
  `meridian.local.toml`) owns Meridian CLI/runtime behavior: timeouts, output,
  state retention, work/context/workspace roots, hooks, harness defaults, and
  primary-session defaults.

Meridian reads agents and skills from project packages first, then user packages
under `~/.meridian/mars/.mars/`, then built-in defaults. Project routing config,
targets, aliases, and the models cache still come from the active Mars root.
Harness-specific directories such as `.claude/`, `.codex/`, `.opencode/`, `.pi/`,
and `.cursor/` are Mars targets, not Meridian discovery roots.

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

A directory with `meridian.toml` or `mars.toml` uses **committed identity**. The
machine-managed ID is stable across moves and is shared by clones and worktrees
when `meridian.toml` is committed. A `mars.toml`-only project is committed but
unminted; its next Meridian write adds the ID to `meridian.toml`.

A directory with neither marker uses **ad-hoc identity**, derived from its
canonical path. Reads create nothing. The first runtime write materializes state
under `~/.meridian/projects/adhoc.<slug>.<hash>/` and default context under the
matching `~/.meridian/context/adhoc.<slug>.<hash>/`; Meridian never writes into
the bare directory. Moving it changes its ad-hoc identity.

`meridian init` and `meridian config init` atomically write the scaffold and a
committed ID. The committed project starts clean: **spawns, sessions, and work
items from before init are not carried over.** The command names the old ad-hoc
root, which remains untouched until retention reclaims it. No state or generated
`.gitignore` lives in a repo-local `.meridian/` directory.

`meridian doctor --global` lists every materialized ad-hoc root with its ID,
origin path, reason, last activity, size, and whether it is prunable. Ad-hoc
roots become prunable after 30 days without a write under their runtime or
context root or a later recorded spawn or session timestamp. Roots whose source
path is gone or now contains a marker use the shorter of that window and 7 days.
Set `state.adhoc_retention_days` in
`~/.meridian/config.toml` (not project config) or use
`MERIDIAN_STATE_ADHOC_RETENTION_DAYS`; `-1` disables ad-hoc pruning and `0`
reclaims as soon as the root is quiescent and its latest activity is over an
hour old.
`--prune` removes only roots reported as prunable, deleting the matching runtime
and context roots together.

## `meridian.toml` Keys

Use `meridian.toml` for Meridian runtime behavior only. Do **not** put
`[agents.<name>]`, `default_model`, or `default_harness` here; those belong in
Mars config.

Canonical keys accepted by `meridian config set/get/reset`:

| Key | Type | Purpose |
|---|---|---|
| `defaults.max_depth` | int | Max zero-based delegated spawn depth |
| `environment.ignore_direnv` | bool | Undo inherited direnv environment changes before harness launch (default `false`) |
| `timeouts.kill_grace_minutes` | float | Grace before force-kill (minutes) |
| `timeouts.guardrail_minutes` | float | Guardrail timeout (minutes) |
| `timeouts.startup_minutes` | float | Startup-phase timeout for backend boot, connection, and session handshake (minutes) |
| `timeouts.wait_minutes` | float | Default `spawn wait` timeout (minutes) |
| `timeouts.pi_child_wave_timeout_seconds` | float | Pi spawn-watch tracked-child wave timeout (seconds; default 300 when unset) |
| `timeouts.resident_deadline_seconds` | float | Child completion-window deadline (seconds; defaults to the calling harness's wait-yield interval) |
| `timeouts.resident_rearm_budget` | int | Maximum resident deadline extensions (nonnegative; unlimited when unset) |
| `timeouts.pi_task_ping_interval_seconds` | float | Pi background-task ping interval (seconds; extension default when unset) |
| `harness.claude` | str | Default model for Claude harness |
| `harness.codex` | str | Default model for Codex harness |
| `harness.opencode` | str | Default model for OpenCode harness |
| `harness.opencode.version` | `auto` \| `v1` \| `v2` | OpenCode backend version (default `auto`) |
| `output.show` | array[str] | Stream categories shown |
| `output.verbosity` | str\|null | `quiet\|normal\|verbose\|debug` |
| `state.retention_days` | int | TTL for stale state pruning (`-1` = never, `0` = immediate, default `30`) |
| `spawn.default_wait_yield_seconds` | float | Default yield interval for `spawn wait` (seconds) |
| `spawn.min_wait_yield_seconds` | float | Minimum yield interval for `spawn wait` (seconds) |
| `primary.autocompact` | int | Context-token compaction threshold for the primary session (minimum 1000) |
| `primary.autocompact_pct` | int | Context-window percentage for primary-session compaction (1–100) |

One machine-wide key is intentionally not exposed through project-scoped
`config set/get/reset`:

| User config key | Type | Purpose |
|---|---|---|
| `state.adhoc_retention_days` | int | TTL for quiescent ad-hoc roots, counting any write under the runtime or context root (`-1` = never, `0` = one hour after the latest activity, default `30`; gone/shadowed paths use the shorter of this value and `7`) |

It is accepted only from `~/.meridian/config.toml` (or `MERIDIAN_CONFIG`) and
`MERIDIAN_STATE_ADHOC_RETENTION_DAYS` for doctor retention decisions. A value
in the current project's `meridian.toml` is ignored by the machine-wide scan.

The following harness runtime-bundle settings are TOML-only; generic
`config set/get/reset` does not yet support them:

| Key | Type | Purpose |
|---|---|---|
| `harness.claude.spawn_watch.enabled` | bool | Loads `meridian-spawn-watch` for interactive Claude primaries (default `true`) |
| `harness.pi.load_all_pi_extensions` | bool | Retain ambient extensions in spawned RPC and scan `extra_extension_paths` in both roles (default `false`; primary always retains native discovery) |
| `harness.pi.extra_extension_paths` | array[str] | Extra extension roots scanned only when `load_all_pi_extensions = true` (default: Pi user extension dir) |
| `harness.pi.background_tasks.enabled` | bool | Toggles `managed-bash` in primary and spawned sessions (`bash` / `bash_manage`, `/ps*`; default `true`) |
| `harness.pi.spawn_watch.enabled` | bool | Toggles the `meridian-spawn-watch` extension (`/spawn` spawn discovery + wait; default `true`) |
| `harness.pi.disable_managed_bash` | bool | **Legacy** — same as `background_tasks.enabled = false` |

Pi's `session-boundary` extension is always loaded in both roles; it has no config
toggle. It records launch-correlated native lifecycle evidence for post-exit session
verification. See the [Pi extension guide](../src/meridian/pi_runtime/README.md).

### OpenCode versions

OpenCode 2.x is the supported target. `harness.opencode.version = "auto"` (the
default) probes `opencode --version`; a 1.x install still works as a frozen
fallback with no new features and push-only idle.

## Ignoring direnv during harness launches

To keep project-local credentials and other direnv exports out of harnesses:

```toml
[environment]
ignore_direnv = true
```

This uses the normal user, project, and local config inheritance. Override it with
`MERIDIAN_IGNORE_DIRENV=true` or `false`, or with `--ignore-direnv` /
`--no-ignore-direnv` on `meridian` and `meridian spawn`. CLI wins over environment,
then local, project, and user config. The generic `meridian config get/set/reset`
commands support `environment.ignore_direnv` through the same catalog entry.

Meridian resolves the setting first, then asks direnv to undo its recorded changes
before constructing the harness environment. Added variables are removed;
overwritten or deleted variables, including `PATH`, are restored. The working
directory stays the same. Explicit launch environment overrides are applied afterward.
The resolved setting is passed to descendants, including when direnv supplied the
setting itself.

When `DIRENV_DIFF` is absent, restoration is a no-op. When it is present, direnv
must be installed and its recorded diff valid; restoration failures stop launch.
The restoration target is `/`, which must contain neither `.envrc` nor `.env`.
This controls inherited environment only: shell hooks or tools that load `.env`
again can reintroduce those values.

## Notifications and idle sessions

Every notification is a hook event: `notify.your_turn` (the idle "your turn"
push), `notify.cache_expiring` (the idle cache warning),
`notify.needs_permission` (a still-waiting Claude permission prompt), and
`notify.manual` (`meridian notify`). The built-in `notify` hook delivers them
through the transports configured in `[notify]`; see
[Routing and extending notifications](#routing-and-extending-notifications) to
change where each one goes or add your own channel.
The `notify.*` prefix means the event carries a notification payload.

### `[notify]`

| Key | Default | Environment variable | Meaning |
|---|---|---|---|
| `push_backend` | `"ntfy"` | `MERIDIAN_NOTIFY_PUSH_BACKEND` | `ntfy` or `none` |
| `email_backend` | `"gmail"` | `MERIDIAN_NOTIFY_EMAIL_BACKEND` | `gmail`, `smtp`, `ntfy`, or `none` |
| `include_messages` | `true` | `MERIDIAN_NOTIFY_INCLUDE_MESSAGES` | Include idle excerpts and permission-prompt text in notifications |
| `ntfy_server` | `"https://ntfy.sh"` | `MERIDIAN_NOTIFY_NTFY_SERVER` | Base URL for the ntfy server |
| `ntfy_topic` | unset | `MERIDIAN_NOTIFY_NTFY_TOPIC` | ntfy topic; an unset topic disables ntfy delivery with a warning |
| `email_to` | unset | `MERIDIAN_NOTIFY_EMAIL_TO` | Email recipient |
| `email_from` | unset (uses `smtp_user`) | `MERIDIAN_NOTIFY_EMAIL_FROM` | Email sender |
| `smtp_user` | unset | `MERIDIAN_NOTIFY_SMTP_USER` | SMTP login; also the default sender |
| `smtp_host` | `"smtp.gmail.com"` | `MERIDIAN_NOTIFY_SMTP_HOST` | SMTP STARTTLS host; the `gmail` backend always uses Gmail's host |
| `smtp_port` | `587` | `MERIDIAN_NOTIFY_SMTP_PORT` | SMTP STARTTLS port; the `gmail` backend always uses port 587 |
| `smtp_password_file` | unset | `MERIDIAN_NOTIFY_SMTP_PASSWORD_FILE` | File containing the SMTP password |

To run your own program on a notification, add a command hook; see
[Routing and extending notifications](#routing-and-extending-notifications).
The ntfy backend tags each message with the event name. Delivery is attempted
once per selected backend; one backend failing does not prevent the other from
running.

Notification titles name the sender: the reason in brackets, then the tmux
session when available, then the agent and harness, as in
`[idle] a2: tech-lead (claude)`. With no agent, the harness appears alone
(`a2: pi`); outside tmux, the terminal prefix is omitted. The other automatic
reasons are `[cache warning]` and `[permission]`. Idle and cache-warning titles
end with the remaining cache lifetime, computed at send time:
`[cache warning] a2: tech-lead (claude) 48 min until cold`, or
`est. 18 min until cold`.
Claude's observed TTL and Pi's Anthropic retention use an unqualified countdown;
OpenAI heuristics and configured/default TTLs use `est.`. An unknown TTL has no
countdown. Under a minute reads `<1 min until cold`; an elapsed deadline reads
`cache cold` (still qualified with `est.` for estimates).

Bodies carry the message: the warning details and message excerpts labelled
`You` and `Assistant`, then a last line naming the sender with its current work
item (`a2: tech-lead (claude) on work: "idle-cache-notify"`, or
`in project: "<project>"` without one). The work item is the session's current
one when the notification is sent.

In permission notices the body contains the harness notification text. With
`include_messages = false`, the body is the generic `Permission prompt waiting`
instead.

A notification names the primary session the human talks to. When a spawned
agent sends one, the title and work item are those of its nearest primary
ancestor, and the body's last line adds the child:
`a2: tech-lead (claude) on work: "x", from child p12 coder (codex)`. When no
primary ancestor can be read, the notification names the spawn itself.

Excerpts are plain text, whitespace-collapsed, and shortened before storage.
Because they leave the machine through ntfy or email, set
`include_messages = false` to omit both. `meridian notify` titles have no reason
tag: the sender, then the first argument (`a2: tech-lead (claude) Review ready`). The body holds any detail,
then the same sender-and-work line.

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
setup with `meridian notify "test" --json`: `"channel": "notify", "status":
"sent", "transports": ["gmail"]` means the default route delivered, and an error starting `gmail:` with
`535` means the app password is wrong or 2-Step Verification is off.

### Routing and extending notifications

Notification events go through the normal [hooks](hooks.md) system: `[[hooks]]`
rows layer user < project < local, `when.agent` matches the notifying
session's agent, `interval` throttles, and `enabled = false` turns a row off.
`MERIDIAN_HOOKS_ENABLED=false` therefore turns off every notification.

**Default routes.** Meridian supplies a hook named `notify` on each notification
event (source `default` in `meridian hooks list`), below every config layer. As
with any hook, a config row with the same name and event replaces it. An
unnamed `builtin = "notify"` row is named `notify`, so such a row for an event,
even with `enabled = false`, replaces the default for that event only; one
without `event` covers every notification event. A row named anything other
than `notify` is an additional route alongside the default. If both routes
select the same push backend, the recipient gets two pushes. To replace rather
than add, omit `name` or set `name = "notify"`, as in the examples below.

The `notify` builtin takes two options:

| Option | Default | Meaning |
|---|---|---|
| `channels` | `["push", "email"]` | `"push"` is `push_backend` and `"email"` is `email_backend`; a backend name (`ntfy`, `smtp`, `gmail`, `none`) sends there directly. |
| `priority` | the notification's | 1–5; overrides the notification's priority |

Unknown options and channel names are config errors. `meridian notify
--no-email`, the idle push, and the permission notice never want email; the
cache warning follows `[idle] warn_email`. Such a notification skips the email
backends (`smtp`, `gmail`) however a route names them.

```toml
# Cache warnings go only to ntfy, at top priority
[[hooks]]
builtin = "notify"
event   = "notify.cache_expiring"
options = { channels = ["ntfy"], priority = 5 }

# No "your turn" push
[[hooks]]
builtin = "notify"
event   = "notify.your_turn"
enabled = false

# Keep idle notices, but silence permission prompts
[[hooks]]
builtin = "notify"
event   = "notify.needs_permission"
enabled = false
```

**Extension channels.** Any `[[hooks]] command = "..."` row on a notification
event receives the notification, so a Slack or desktop channel is one row and
one script. Stdin carries the hook payload (`schema_version` 3) with a
`notification` object: `title`, `body`, `priority`, `email`, and `label`
(`tmux_session`, `project`, `work_id`, `agent`, `harness`, `child_spawn_id`, `child_agent`, `child_harness`, `sender`, `headline`), and
`attachments` (absolute paths from `meridian notify --attach`). The
environment adds `MERIDIAN_NOTIFICATION_TITLE`, `MERIDIAN_NOTIFICATION_BODY`
and `MERIDIAN_NOTIFICATION_PRIORITY` to the usual `MERIDIAN_HOOK_*` variables.

```toml
[[hooks]]
name    = "desktop"
command = "./scripts/desktop-notify.sh"   # e.g. notify-send "$MERIDIAN_NOTIFICATION_TITLE" "$MERIDIAN_NOTIFICATION_BODY"
event   = "notify.manual"
```

Notification routes run even in a directory with no Meridian runtime state;
only `interval`-throttled hooks skip there. `meridian notify --json` reports one
result per route, named by the hook: a route that delivered nothing (every
backend `none`, or skipped by `when`) is `none`, and a failed `notify` route's
error names each transport that failed. Sent routes list the transports that
delivered. If hook rows are invalid, notification emission drops and names only
those rows, keeps all valid rows (including silencing rows and command routes),
then applies the usual default-route overrides. The warning is part of both
text and JSON reports. `meridian hooks check` still exits non-zero and names
every invalid row.

### `[idle]`

Idle handling sends two notices, independently: the waiting push ("your turn")
and a cache-expiry warning. Meridian does not compact idle sessions; harnesses
that compact on their own (Claude Code 2.1.299+, Codex, OpenCode, Pi) keep doing
so, and Meridian leaves those settings alone.

Claude also reports a permission prompt that remains unanswered long enough for
Claude's own notification to fire. Meridian forwards that signal once as
`notify.needs_permission`, without reading or writing the idle stretch, holding
the waiting push, or tracking the answer. Only Claude produces this event today.
It follows the same `[idle] enabled` switch as the waiting push; a hook row can
silence only the permission notice.

| Key | Default | Environment variable | Meaning |
|---|---:|---|---|
| `enabled` | `true` | `MERIDIAN_IDLE_ENABLED` | Enable automatic idle handling |
| `push_seconds` | `60` | `MERIDIAN_IDLE_PUSH_SECONDS` | Seconds after a long wait before the waiting push |
| `long_turn_seconds` | `120` | `MERIDIAN_IDLE_LONG_TURN_SECONDS` | Minimum wait that uses `push_seconds` |
| `quick_turn_push_seconds` | `600` | `MERIDIAN_IDLE_QUICK_TURN_PUSH_SECONDS` | Seconds after a quick or unknown-length turn before the waiting push |
| `warn_minutes` | `15` | `MERIDIAN_IDLE_WARN_MINUTES` | Minutes before cache expiry to warn |
| `warn_email` | `true` | `MERIDIAN_IDLE_WARN_EMAIL` | Let the warning reach email channels |
| `late_fire_tolerance_seconds` | `120` | `MERIDIAN_IDLE_LATE_FIRE_TOLERANCE_SECONDS` | Skip a timer that fires more than this many seconds late |

When you have waited at least two minutes since your last prompt, the waiting
push follows 60 seconds after the turn ends; after a quick or unknown-length
turn, it waits 10 minutes. Set `long_turn_seconds = 0` to push after every turn
using `push_seconds`. Codex observes the user return only when that turn ends,
so its turn length is unknown and uses the 10-minute delay. The warning is
scheduled `warn_minutes` before the cache TTL expires; a push that would not
land strictly before the warning is dropped. An unknown TTL produces only the
push.

#### Running work

The push means the agent is done and waiting on you, so it reads the work a
turn left running:

- Subagents and workflows hold the push. A background shell does too only when
  its final command segment is a foreground `meridian spawn` creation or
  `meridian spawn wait`, with no `while`, `until`, `for`, or `select` segment.
  Semicolons, pipelines, background operators, subshell parentheses, and
  unescaped, unquoted newlines separate command segments; redirections stay
  with their command. Direct paths, leading environment assignments, `uv run ...
  meridian`, and `uvx meridian` are recognized. Earlier setup such as `cd x &&`
  is fine. That work ends and wakes the session, and that turn arms again; the
  response stays owed until its push is sent, so a long wait uses `push_seconds`
  even when an earlier scheduled push was replaced.
- A certain `meridian spawn wait` has a latest wake time. An explicit
  `--timeout` is minutes and disables yielding; otherwise Meridian uses
  `--yield-after-secs` or the harness's wait-yield default (3000 seconds for
  Claude). If that wake lands before the cache goes cold, the cache warning is
  dropped, since the wake keeps the cache warm; otherwise the warning stays.
- Detached spawn creation (`--bg`), spawn management commands such as
  `list`/`show`/`cancel`, loops, commands followed by more work, and any other
  background shell or task may never end. They only delay the push to at least
  `quick_turn_push_seconds`.
- Monitors are ignored.

Otherwise the warning arms after every turn, even while the push is held.
Claude, Pi, and OpenCode 2.x report running work using bounded snapshots so a
large task list cannot exceed the operating system's argument limit. OpenCode
reports only the shells and subagents its agent started in the background, the
work it wakes the session for; a shell you start yourself is not reported. A
subagent waiting on a permission prompt is not counted until you answer it;
answering counts as your return, so its next prompt or its result gets its own
push. Codex and OpenCode 1.x primaries push as if nothing were running.

### `[harness.<harness>.idle]`

Use `claude`, `codex`, `opencode`, or `pi` for `<harness>`.

| Key | Default | Environment variable |
|---|---|---|
| `enabled` | `true` | `MERIDIAN_HARNESS_IDLE_ENABLED_<H>` |
| `ttl_seconds` | Claude/OpenCode/Pi: sensed; Codex: `1800` | `MERIDIAN_HARNESS_IDLE_TTL_SECONDS_<H>` |

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
`MERIDIAN_IDLE_ENABLED=0` always wins, even over a
`[harness.<harness>.idle] enabled` file setting.

### Default behavior per harness

| Harness | Default behavior |
|---|---|
| Claude | Push and warning against the cache TTL sensed from the transcript (one hour or five minutes) |
| Pi | Push and warning for `anthropic`, `openai-codex`, and `openai` (long retention only). Meridian sets `PI_CACHE_RETENTION=long` for interactive primaries unless you set it. Other providers are push-only. |
| Codex | Push and warning with a fixed 1800-second TTL |
| OpenCode | 2.x `openai`: push and warning against an estimated 30-minute TTL. Other providers are push-only. 1.x remains push-only ([OpenCode versions](#opencode-versions)). |

The OpenAI estimates (Pi `openai-codex`, OpenCode `openai`) are per provider, not
per model, and assume GPT-5.6 or later. For an older model on those routes, set
`ttl_seconds` yourself.

The Pi cache-retention default is launch behavior, independent of whether
`[idle]` is enabled: an interactive Pi session gets the longer provider cache
even when idle notifications are off.

Idle automation runs only in an interactive primary session. Each stage is
claimed at most once between user prompts, and a stale or late timer is
skipped. A positively identified user prompt closes the idle stretch
immediately. Reopening a primary through Meridian counts as a user return:
before the harness starts, Meridian closes any earlier idle stretch for that
session, so its push and warning never fire. This does not cover a Claude TUI
started outside Meridian or switching sessions inside a running Claude TUI with
`/resume`.

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
Which of a profile's `tools`, `sandbox`, and `approval` each harness applies is in
[Permissions by harness](agent-profiles.md#permissions-by-harness).

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

[harness.claude.spawn_watch]
enabled = true

[output]
show = ["lifecycle", "error"]
verbosity = "verbose"

[primary]
autocompact_pct = 70

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
