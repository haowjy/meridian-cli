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

The following Pi extension settings are TOML-only; generic `config set/get/reset`
does not yet support them:

| Key | Type | Purpose |
|---|---|---|
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

Idle notification titles put the reason first, followed by the tmux session
when available, the agent (or harness), and an explicitly labelled work item:
`[idle] a2: pi on work: "idle-cache-notify"`. Other reasons are `[cache warning]`,
`[compacted]`, `[compaction failed]`, and `[compaction skipped]`. Without a work
item, the title uses `in project: "<project>"`; outside tmux, it omits the terminal
prefix. Idle and cache-warning titles also show the remaining cache lifetime,
computed at send time: `— 48 min until cold` or `— est. 18 min until cold`.
Claude's observed TTL and Pi's Anthropic retention use an unqualified countdown;
OpenAI heuristics and configured/default TTLs use `est.`. An unknown TTL has no
countdown. Under a minute reads `<1 min until cold`; an elapsed deadline reads
`cache cold` (still qualified with `est.` for estimates). Compaction-result titles
omit the old countdown, since that operation can refresh the cache.

Bodies retain the warning or result details and message excerpts labelled
`You` and `Assistant`, without repeating the tmux session.

Excerpts are plain text, whitespace-collapsed, and shortened before storage.
Because they leave the machine through ntfy or email, set
`include_messages = false` to omit both. For `meridian notify`, `--title`
replaces the headline and moves the terminal/agent/work description into the
body so the context remains visible.

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
| `background_push_seconds` | `900` | `MERIDIAN_IDLE_BACKGROUND_PUSH_SECONDS` | Minimum push delay while other background tasks (a dev server, a build) still run |
| `warn_minutes` | `15` | `MERIDIAN_IDLE_WARN_MINUTES` | Minutes before cache expiry to warn |
| `warn_email` | `true` | `MERIDIAN_IDLE_WARN_EMAIL` | Include the configured email backend in the warning |
| `compact_minutes` | `5` | `MERIDIAN_IDLE_COMPACT_MINUTES` | Minutes before cache expiry to try compaction |
| `compact` | `false` | `MERIDIAN_IDLE_COMPACT` | Permit idle compaction |
| `compact_min_growth_tokens` | break-even | `MERIDIAN_IDLE_COMPACT_MIN_GROWTH_TOKENS` | Override the calculated break-even with a fixed growth floor; `0` permits any known growth |
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

The waiting push and the cache warning are independent. The push means the
agent is done and waiting on you, so a Claude primary skips it while work that
will wake the session is still running: native subagents and workflows,
background shells running `meridian spawn`, and active child spawns. The turn
that work wakes arms again, timed from your last prompt, so a push follows a
minute after it if nothing else is running. Other background tasks only stretch
the push to at least `background_push_seconds`, since a dev server may never
end; Claude's Monitor tasks are ignored. The cache warning is armed after every
turn regardless, except while a `meridian spawn wait` that yields before the
cache goes cold is running: that wait wakes the session and re-warms the cache
itself.

### `[harness.<harness>.idle]`

Use `claude`, `codex`, `opencode`, or `pi` for `<harness>`.

| Key | Default | Environment variable |
|---|---|---|
| `enabled` | `true` | `MERIDIAN_HARNESS_IDLE_ENABLED_<H>` |
| `compact` | `false` | `MERIDIAN_HARNESS_IDLE_COMPACT_<H>` |
| `ttl_seconds` | Claude/OpenCode/Pi: sensed; Codex: `1800` | `MERIDIAN_HARNESS_IDLE_TTL_SECONDS_<H>` |
| `compact_min_growth_tokens` | global setting | `MERIDIAN_HARNESS_IDLE_COMPACT_MIN_GROWTH_TOKENS_<H>` |

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

Idle compaction is off by default; set `compact = true` (globally or per
harness) to use it. The table describes behavior with it on. Claude Code
2.1.299+ has its own idle compaction for large sessions (`/config` → Idle
compaction); Meridian leaves that setting alone.

| Harness | Default behavior |
|---|---|
| Claude | Push and warning; compact after roughly 50k–63k tokens of growth for a one-hour cache (higher for shorter caches) |
| Pi | Push and warning. Compaction does not pay at measured prices, so the compact stage records `skipped:not-worth-it`. Meridian still sets `PI_CACHE_RETENTION=long` for interactive primaries unless you set it; unsupported providers remain push-only. |
| Codex | Push and warning with a fixed 1800-second TTL; compact after roughly 100k tokens of growth, assuming the OpenAI/ChatGPT route |
| OpenCode | 2.x `openai`: push and warning; compact after roughly 60k tokens of growth from a new baseline, or 44k after Meridian's own compaction. Other providers are push-only. 1.x remains push-only ([OpenCode versions](#opencode-versions)). |

The OpenAI estimates (Pi `openai-codex`, OpenCode `openai`) are per provider, not
per model, and assume GPT-5.6 or later. For an older model on those routes, set
`ttl_seconds` yourself.

The Pi cache-retention default is launch behavior, independent of whether
`[idle]` is enabled: an interactive Pi session gets the longer provider cache
even when idle notifications and compaction are off.

Idle automation runs only in an interactive primary session. Each stage is
claimed at most once between user prompts. At the compact deadline Meridian
compares growth since the first trustworthy context observation with the cost
of compacting now. The default threshold is the per-harness break-even:

```text
growth needed = (return_probability × cold_read_cost × residue
                 + compact_read_cost × baseline
                 + uncached_input + output_cost × summary)
                / (return_probability × cold_read_cost - compact_read_cost)
```

The baseline survives user returns and new idle stretches. It resets when the
context shrinks, and the first real turn after a Meridian compaction establishes
a new baseline. `meridian idle status --json` exposes the baseline in `context`
and the current stretch's `compact_eval`; the table shows a skipped comparison such as
`skipped:not-worth-it (19k/50k)`.

Set `compact_min_growth_tokens` to replace the calculated threshold. This also
opts routes whose measured economics say "never" back into compaction. The
override still requires a known current context and baseline.

Pi registers no cost constants: at the fixed return probability of 0.5 its
measured compactions never pay. Its evaluations report `basis: never` and
`costs: null` unless a growth override is set. OpenCode 2.x uses measured
OpenAI-route costs; 1.x has no detected TTL or context accounting and remains
push-only. Codex and OpenCode cost resolution does not detect custom model
providers at fire time. A manually configured TTL on another provider therefore
uses the OpenAI assumptions; set a growth override when that route differs.

A compaction is skipped when the timer is stale or late, idle or compaction is
disabled, the cache is already cold, the harness is busy, an agent or child
spawn is still running, growth is not worth the cost, the context or baseline
is unknown, or the harness's own automatic compaction is disabled. Unsent
composer text does not block compaction; Codex clears it before submitting
`/compact`. Before sending any key, Meridian retries an unrecognized Codex
screen for about 10 seconds. If it still cannot verify the focused composer,
it sends no keys, skips the attempt, and sends a push-only notice with the
remaining cache lifetime. A failed primer or clear verification likewise stops
the sequence and sends the same notice. A positively identified user prompt
closes the idle stretch immediately.
Reopening a primary through Meridian counts as a user return: before the harness
starts, Meridian closes any earlier idle stretch for that session, so its push,
warning, and compaction never fire. This does not cover a Claude TUI started
outside Meridian or switching sessions inside a running Claude TUI with `/resume`.

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
