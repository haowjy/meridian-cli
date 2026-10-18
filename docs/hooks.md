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

# Trigger with a specific event context
meridian hooks run sync-notes --event work.done
```

`hooks run` bypasses interval throttling. When a name is registered for more
than one event, `--event` is required to select the row. The `notify` builtin
cannot run manually because it has no notification payload; use
`meridian notify` instead.

## Events

| Event | Class | Fires when |
| ----- | ----- | ---------- |
| `spawn.created` | observe | A spawn is registered in the database |
| `spawn.running` | observe | A spawn's harness process starts |
| `spawn.start` | observe | Alias for `spawn.running` |
| `spawn.finalized` | post | A spawn reaches a terminal state (`succeeded`, `failed`, or `cancelled`) |
| `work.start` | observe | A work item is switched to (becomes active) |
| `work.started` | observe | Alias for `work.start` |
| `work.done` | post | A work item is completed |
| `session.responded` | observe | An idle primary sends its "your turn" push |
| `session.cache_expiring` | observe | An idle primary warns that its prompt cache is about to go cold |
| `notify.manual` | observe | `meridian notify` runs |

The last three are notification events: their context carries a
`notification` (title, body, priority, email, session label, attachment paths). Meridian routes
each one to the built-in `notify` hook unless config routes it; see
[Builtin: `notify`](#builtin-notify).

Event class affects default timeout and failure policy: `observe` events default to 30s / `warn`; `post` events default to 60s / `warn`.

A single hook row can register for multiple events. When a builtin supplies default events and no `event` field is set, one hook registration is created per default event. Setting `event` explicitly overrides this and registers for only that one event.

## Configuring Hooks

Hooks live in any Meridian config file. Precedence is: `default < user < project < local`, where `default` is Meridian's own `notify` routes. A row with the same name and event as a lower layer's replaces it.

```toml
# meridian.toml (project) or ~/.meridian/config.toml (user)

[[hooks]]
name    = "sync-notes"
builtin = "git-autosync"
remote  = "git@github.com:team/docs.git"

[[hooks]]
name    = "run-tests"
command = "make test"
event   = "spawn.finalized"
```

### Hook Schema

| Field | Type | Required | Default | Purpose |
| ----- | ---- | -------- | ------- | ------- |
| `name` | str | yes (unless `builtin`) | builtin name or synthesized builtin identity | Unique identifier; used by `hooks run NAME` |
| `builtin` | str | one of `builtin`/`command` | — | Use a builtin hook |
| `command` | str | one of `builtin`/`command` | — | Shell command to run |
| `event` | str | yes (unless builtin supplies defaults) | builtin default | Event that triggers the hook |
| `remote` | str | required by `git-autosync` | — | Git remote URL for hooks that operate on a remote repo |
| `enabled` | bool | no | `true` | Set to `false` to disable without removing |
| `priority` | int | no | `0` | Lower values run first |
| `failure_policy` | str | no | builtin default | `fail` \| `warn` \| `ignore` |
| `timeout_secs` | int | no | builtin default | Max seconds before hook is killed |
| `interval` | str | no | builtin default | Minimum time between runs, e.g. `30s`, `5m`, `2h` |
| `require_serial` | bool | no | `false` | Block concurrent hook executions |
| `when.status` | array[str] | no | — | Only fire when spawn exits with one of these statuses |
| `when.agent` | str | no | — | Only fire for this agent profile (for notification events, the notifying session's agent) |
| `exclude` | array[str] | no | — | Skip for these agent profiles |
| `options.conflict_policy` | str | no | `"abort"` | `git-autosync` only: `"abort"` aborts a conflicted sync; `"leave"` keeps conflicts for review |
| `options.channels` | array[str] | no | `["push", "email"]` | `notify` only: transports to deliver to |
| `options.priority` | int | no | notice's | `notify` only: 1–5 priority override |

`command` and `builtin` are mutually exclusive.

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
event   = "spawn.finalized"
```

Inline commands are fine when they remain short and readable.

Command hooks receive the event context as JSON on stdin (`schema_version` 2)
and as `MERIDIAN_HOOK_*` and related environment variables. Hooks run even in a
directory with no Meridian runtime state; only `interval`-throttled hooks skip
there. `MERIDIAN_HOOKS_ENABLED=false` turns off all hook dispatch, including
notification delivery.

## Builtin: `notify`

`notify` delivers a notification event through the transports configured in
`[notify]`. Options `channels` (default `["push", "email"]`, or backend names
such as `ntfy`) and `priority` (1–5) pick where and how loud.

**Default events:** `session.responded`, `session.cache_expiring`, `notify.manual`

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
event   = "session.responded"
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

`git-autosync` keeps a remote Git repo in sync with local changes. On each trigger it:

1. Stages and commits any uncommitted changes in the target repo
2. Fetches upstream
3. Rebases when behind remote
4. Pushes when ahead or after a local commit

By default, a conflicted sync is aborted, returning the repository to its
pre-sync state. Set `conflict_policy = "leave"` to keep the merge or rebase in
place so agents or humans can inspect and resolve it. Future autosync runs skip
all git operations until that conflict is resolved.

```toml
# Leave conflict markers for manual resolution
[[hooks]]
builtin = "git-autosync"
remote  = "git@github.com:team/docs.git"
[hooks.options]
conflict_policy = "leave"
```

**Required:** `remote` — the Git remote URL of the repo to sync.

**Default events:** `spawn.start`, `spawn.finalized`, `work.started`, `work.done`

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
meridian hooks run sync-notes --event work.done
```

## MCP

Hook commands (`hooks list`, `hooks check`, `hooks run`) are CLI-only and not exposed via MCP. The MCP server exposes `extension_list_commands` and `extension_invoke` — hook state is accessible through extension commands if registered.

See [mcp-tools.md](mcp-tools.md) for the MCP tool reference.
