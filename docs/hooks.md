# Hooks

Hooks let you run commands or builtins automatically when Meridian events fire — syncing a remote repo, running checks after a spawn finishes, delivering notifications, or any custom script you want wired to the agent lifecycle.

## Commands

| Command | Description |
| ------- | ----------- |
| `meridian hooks list` | Show all registered hooks and their resolved configuration |
| `meridian hooks check` | Validate hook config and exit non-zero on errors |
| `meridian hooks run NAME` | Execute a hook manually, bypassing interval throttling |
| `meridian hooks run NAME --event EVENT` | Execute with a specific event context |

```bash
# See all hooks, including builtins and Meridian's default routes, with their sources
meridian hooks list

# Validate config before committing changes
meridian hooks check

# Trigger a hook manually
meridian hooks run sync-notes
```

`hooks run` bypasses interval throttling. Registrations with the same name,
builtin, command, options, timeout and failure policy are one hook: without
`--event` it runs once, as its last registered event (an unevented
`git-autosync` row runs as `work.done`), and `--event` may name any event, as
for a single-row hook. When those fields differ, use `--event` to select a row. A builtin that only supports notification events cannot run
manually because it has no notification payload; use `meridian notify` instead.

## Events

| Event | Class | Fires when |
| ----- | ----- | ---------- |
| `spawn.created` | observe | A persisted spawn is announced to lifecycle observers |
| `spawn.started` | observe | A spawn enters `running`, including when its initial persisted state is already `running` |
| `spawn.finished` | post | A terminal spawn state (`succeeded`, `failed`, `cancelled`, or `timed_out`) is persisted |
| `work.created` | observe | `meridian work start` creates a new work item |
| `work.activated` | observe | A session chooses a work item through `work start`, `work switch`, or a primary launch with `--work` or work carried by `--from` |
| `work.done` | post | `meridian work done`, or an update to `done`, archives a work item |
| `notify.your_turn` | observe | The idle service sends its scheduled "your turn" notification |
| `notify.cache_expiring` | observe | The idle service sends its scheduled prompt-cache warning |
| `notify.manual` | observe | `meridian notify` emits its notification |

Every `notify.*` event carries a
`notification` (title, body, priority, email, session label, attachment paths). Meridian routes
each one to the built-in `notify` hook unless config routes it; see
[Builtin: `notify`](#builtin-notify).

Event class affects default timeout and failure policy: `observe` events default to 30s / `warn`; `post` events default to 60s / `warn`.

A single hook row can register for multiple events. When a builtin supplies default events and no `event` field is set, one hook registration is created per default event. Setting `event` explicitly overrides this and registers for only that one event.

## Configuring Hooks

Hooks live in any Meridian config file. Precedence is: `default < user < project < local`, where `default` is Meridian's own `notify` routes. A row with the same name and event as a lower layer's replaces it. Execution is ordered by source first (`default`, then `user`, `project`, and `local`), then by descending priority within that source, then by declaration order. A priority value never moves a hook ahead of a hook from an earlier source.

```toml
# meridian.toml (project) or ~/.meridian/config.toml (user)

[[hooks]]
name    = "sync-notes"
builtin = "git-autosync"
remote  = "git@github.com:team/docs.git"

[[hooks]]
name    = "run-tests"
command = "make test"
event   = "spawn.finished"
```

### Hook Schema

| Field | Type | Required | Default | Purpose |
| ----- | ---- | -------- | ------- | ------- |
| `name` | str | yes (unless `builtin`) | builtin name or synthesized builtin identity | Unique identifier; used by `hooks run NAME` |
| `builtin` | str | one of `builtin`/`command` | — | Use a builtin hook |
| `command` | str | one of `builtin`/`command` | — | Shell command to run |
| `event` | str | yes (unless builtin supplies defaults) | builtin default | Event that triggers the hook |
| `remote` | str | no | — | `git-autosync` shorthand for `options.remote`; ignored when `options.remote` is set |
| `enabled` | bool | no | `true` | Set to `false` to disable without removing |
| `priority` | int | no | `0` | Higher values run first within the same source |
| `failure_policy` | str | no | builtin default | `fail` \| `warn` \| `ignore` |
| `timeout_secs` | int | no | builtin default | Max seconds before hook is killed |
| `interval` | str | no | builtin default | Minimum time between runs, e.g. `30s`, `5m`, `2h` |
| `require_serial` | bool | no | `false` | Block concurrent hook executions |
| `when.status` | array[str] | no | — | On `spawn.finished`, fire only for these outcomes: `success`, `failure`, `cancelled`, `timeout` (`succeeded`, `failed`, `cancelled`, `timed_out`) |
| `when.agent` | str | no | — | Only fire for this agent profile (for notification events, the notifying session's agent) |
| `when.kind` | array[str] | no | — | On spawn events, only fire for these kinds: `child`, `primary`, `streaming`; notification events use the notifying spawn's kind when known; work events reject this condition |
| `exclude` | array[str] | no | — | `git-autosync` file path patterns to leave out of commits and stash around merges |
| `options.remote` | str | no | — | Git remote URL for `git-autosync` to clone and sync |
| `options.path` | str | no | — | Local repository for `git-autosync` to initialize and commit without remote sync |
| `options.conflict_policy` | str | no | `"abort"` | `git-autosync` only: handling for a merge or rebase already in progress; `"abort"` clears it and continues, `"leave"` skips the sync |
| `options.channels` | array[str] | no | `["push", "email"]` | `notify` only: transports to deliver to |
| `options.priority` | int | no | notice's | `notify` only: 1–5 priority override |

`command` and `builtin` are mutually exclusive.

Conditions that an event can never supply are rejected when config loads:
`when.status` is limited to `spawn.finished`, and work events cannot use
`when.agent` or `when.kind`. A runtime context that lacks or does not match a
configured condition skips the hook.

If you omit `name` for a builtin hook, Meridian names it from the options that
pick its target: `git-autosync` rows from `remote` or `path`, so rows for
different repos don't override each other; `notify` rows are all `notify`.
For predictable `hooks run NAME` usage, set `name` explicitly, except when a
`notify` row is meant to replace the default route as described below.

### Shell Behavior

Hook `command` strings are executed via `sh -c`. For non-trivial shell logic,
use a script file instead of embedding it in TOML:

```toml
[[hooks]]
name    = "check"
command = "./scripts/check.sh"
event   = "spawn.finished"
```

Inline commands are fine when they remain short and readable.

Command hooks receive the event context as JSON on stdin (`schema_version` 3)
and as `MERIDIAN_HOOK_*` and related environment variables. Hooks run even in a
directory with no Meridian runtime state; only `interval`-throttled hooks skip
there. `MERIDIAN_HOOKS_ENABLED=false` turns off all hook dispatch, including
notification delivery.

## Builtin: `notify`

`notify` delivers a notification event through the transports configured in
`[notify]`. Options `channels` (default `["push", "email"]`, or backend names
such as `ntfy`) and `priority` (1–5) pick where and how loud.

**Default events:** `notify.your_turn`, `notify.cache_expiring`, `notify.manual`

The `notify` builtin accepts only those notification events; configuring it on
another event is a hook config error.

Its `HookResult.details["transports"]` value lists the backend names that
delivered. Other builtins need not produce that key.

Meridian supplies a hook named `notify` on each of these events (source
`default` in `hooks list`). An unnamed `builtin = "notify"` row is named
`notify` too, so a row for an event, even with `enabled = false`, replaces only
that event's default. A row named anything other than `notify` adds a second
route; if it selects the same backend as the default, the recipient gets the
push twice. Omit `name` (as below), or set `name = "notify"`, to replace the
default instead.

```toml
# Silence the "your turn" push
[[hooks]]
builtin = "notify"
event   = "notify.your_turn"
enabled = false
```

A `command` hook on a notification event is an extra channel: it gets the
`notification` object on stdin and `MERIDIAN_NOTIFICATION_TITLE`, `_BODY`, and
`_PRIORITY` in its environment. See
[Routing and extending notifications](configuration.md#routing-and-extending-notifications)
for channel semantics and examples.

When notification emission finds invalid hook rows, it drops and names only
those rows, retains every valid row, and applies default-route overrides as
usual. `meridian hooks check` instead exits non-zero and names every invalid
row.

## Builtin: `git-autosync`

`git-autosync` commits changes in a local repository and can keep it in sync
with a remote. In remote mode, each trigger:

1. Stages and commits uncommitted changes except paths matching `exclude`
2. Fetches upstream
3. Merges `origin/<current-branch>` when behind remote
4. Pushes when ahead or after a local commit

If the merge conflicts, autosync records the conflict and aborts the merge,
returning the repository to its pre-merge state. A later trigger retries the
commit/fetch/merge workflow. If that merge conflicts while any unresolved
conflict record already exists for the sync root, autosync aborts it without
writing another record.

At the start of a run, autosync also detects a merge or rebase already in
progress. The default `conflict_policy = "abort"` aborts that operation and
continues the sync. Set it to `"leave"` to keep the pre-existing operation in
place and skip that sync. Autosync itself never rebases.

```toml
# Leave a pre-existing merge or rebase in place for manual resolution
[[hooks]]
builtin = "git-autosync"
remote  = "git@github.com:team/docs.git"
[hooks.options]
conflict_policy = "leave"
```

**Target:** configure at least one of top-level `remote`, `options.remote`, or
`options.path`. A remote selects clone/fetch/merge/push mode; otherwise
`options.path` selects local commit-only mode. `options.remote` overrides the
top-level shorthand, and remote mode wins when a path is also present.

**Default events:** `spawn.finished`, `work.activated`, `work.done`

Because `work.activated` records a session choosing work, the default also syncs after
`meridian work switch` successfully updates a session.

```toml
[[hooks]]
name    = "sync-notes"
builtin = "git-autosync"
remote  = "git@github.com:team/notes.git"
```

To register only for specific events, set `event` explicitly:

```toml
# Only sync when a work item completes
[[hooks]]
name    = "sync-notes"
builtin = "git-autosync"
remote  = "git@github.com:team/notes.git"
event   = "work.done"
```

To run manually at any time:

```bash
meridian hooks run sync-notes
```

## MCP

Hook commands (`hooks list`, `hooks check`, `hooks run`) are CLI-only and not exposed via MCP. The MCP server exposes `extension_list_commands` and `extension_invoke` — hook state is accessible through extension commands if registered.

See [mcp-tools.md](mcp-tools.md) for the MCP tool reference.
