# Hooks

This page covers Meridian's own event hooks. Mars packages can also install harness hooks (Claude, Codex, OpenCode, Pi); see [Mars configuration](configuration/mars.md#what-a-package-installs).

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
builtin, command, options and timeout are one hook: without
`--event` it runs once, as its last registered event (an unevented
builtin row runs as the last of its default events), and `--event` may name any event, as
for a single-row hook. When those fields differ, use `--event` to select a row. A builtin that only supports notification events cannot run
manually because it has no notification payload; use `meridian notify` instead.
A row on a [machine event](#machine-events) cannot run manually either: it has no
message to receive.

## Events

| Event | Default timeout | Fires when |
| ----- | --------------- | ---------- |
| `spawn.created` | 30s | A spawn record is first written (new spawns are created `running`) |
| `spawn.started` | 30s | A run starts `running`: with `spawn.created` for a new spawn, alone for each `--continue` of it |
| `spawn.finished` | 60s | A run's terminal state (`succeeded`, `failed`, `cancelled`, or `timed_out`) is persisted, once per run; a continued spawn fires it again under the same `spawn_id` with a new `event_id` |
| `work.created` | 30s | `meridian work start` creates a new work item |
| `work.activated` | 30s | `work start` or `work switch` resolves an item, with or without a session, or a primary launch attaches `--work` / `--from` work |
| `work.done` | 60s | `meridian work done`, or an update to `done`, archives a work item |
| `notify.your_turn` | 30s | The idle service sends its scheduled "your turn" notification |
| `notify.cache_expiring` | 30s | The idle service sends its scheduled prompt-cache warning |
| `notify.needs_permission` | 30s | Claude reports a permission prompt that is still waiting |
| `notify.manual` | 30s | `meridian notify` emits its notification |
| `inbox.received` | 60s | A message is filed in this machine's inbox, by `meridian message inbox` here or `meridian message <machine>/inbox` from another machine; a [machine event](#machine-events) |

Every `notify.*` event carries a
`notification` (title, body, priority, email, session label, attachment paths). Meridian routes
each one to the built-in `notify` hook unless config routes it; see
[Builtin: `notify`](#builtin-notify).

### Machine events

`inbox.received` belongs to the machine, not to a project. Its rows differ from
every other event's in four ways:

- **Only the user config supplies them** (`~/.meridian/config.toml`, or the file
  `MERIDIAN_CONFIG` names). A project's `meridian.toml` and `meridian.local.toml`
  are not read when the event fires, so a repository cannot add a row, replace
  yours by name, or stop the event with a broken file. An `inbox.received` row in
  a project file is an invalid row: `hooks list` and `hooks check` report it, and
  the project's other hooks keep running without it.
- **No project in the context.** `project_root` and `runtime_root` are `null` on
  stdin, `MERIDIAN_PROJECT_DIR` is unset, and the command runs in your home
  directory. The context carries `inbox_message.id` and `inbox_message.path`,
  also set as `MERIDIAN_INBOX_MESSAGE_ID` and `MERIDIAN_INBOX_MESSAGE_PATH`. The
  file at that path is the message as JSON: `text`, `from`, `received`, and when
  set `project`, `reply_to` and `via`. `from` has `kind` (`session` or
  `terminal`), the sender's `machine` (its name) and `machine_id`, and for a
  session its `id` (the chat ID), its `project` when it has one, and its `agent`
  when known: a session on another machine sends none. The text is another party's words: a row that
  hands it to a program must not run it as a command.
- **No session in the environment.** The rows run in a process detached from the
  command that filed the message, with that command's environment minus the names
  Meridian injects into a session (`MERIDIAN_SPAWN_ID`, `MERIDIAN_CHAT_ID`,
  `MERIDIAN_PROJECT_DIR` and the rest). `MERIDIAN_HOME`, `MERIDIAN_CONFIG` and
  other config inputs stay. `PATH` is the filing command's, so a program the row
  runs must be on it. For a message filed from another machine that is the `PATH` of
  an SSH command, which leaves out what your shell profile adds; Meridian puts its own
  directory first, so `meridian` is always found.
- **No `interval`.** A row with one is invalid: the event has no project state to
  record the last run in.

The command that files the message does not wait for the rows, and their output
goes nowhere. What each row did is written to
`~/.meridian/machine/inbox/hook-log/<message id>.json`: each row's name, outcome,
exit code and the last 1 KiB each of its standard output and standard error, plus
the errors that stopped a row from loading or running. A row that files a message
does not fire the event again.

```toml
# ~/.meridian/config.toml
[[hooks]]
name    = "offer-to-dispatcher"
event   = "inbox.received"
command = "meridian inbox offer \"$MERIDIAN_INBOX_MESSAGE_ID\" c20417"
```

`c20417` stands for the chat ID of a primary session that is running: in that
session, `echo $MERIDIAN_CHAT_ID` prints it. `offer` refuses any other session,
and the refusal is in the hook log. The row does nothing once that session has
exited or moved to a new chat, so change the ID when you start the next one.

Hook failures always log and continue to the next hook; they never undo or stop
the event that fired them.

Spawn event IDs are stable for a given run of a spawn and event, so a second
`spawn.finished` delivery after an authoritative terminal rewrite of the same run has the same
ID and can be deduplicated; a continue's run has its own. Work and notification event IDs are unique for each
firing, so repeated activations of the same work item remain distinct.

A primary launch with `--work`, or with work inherited through `--from`, fires
`work.activated` synchronously after its session is created and before the
harness starts. A hook on that event therefore delays primary startup. Child work
inheritance, work rename attachment maintenance, and `work clear` do not fire
the event.

A single hook row can register for multiple events. When a builtin supplies default events and no `event` field is set, one hook registration is created per default event. Setting `event` explicitly overrides this and registers for only that one event.

The former event names `spawn.start`, `spawn.running`, `spawn.finalized`,
`work.start`, `work.started`, `session.responded`, and `session.cache_expiring` are
rejected with an error that names the replacement. See
[Removed hook names and keys](upgrading.md#removed-hook-names-and-keys).

### Telemetry and debug output

Hook events and `meridian telemetry query` are different projections.
`spawn.created` and `spawn.started` reach hooks and core lifecycle observers;
with `MERIDIAN_DEBUG=1`, the debug observer writes them to stderr. The local
telemetry sink intentionally keeps only `spawn.process_exited` and terminal
outcome records for spawns. Work activation remains authoritative in work and
session state; its telemetry envelope is best-effort, like other process
telemetry.

## Configuring Hooks

Hooks live in any Meridian config file. Precedence is: `default < user < project < local`, where `default` is Meridian's own `notify` routes. A row with the same name and event as a lower layer's replaces it. Execution is ordered by source first (`default`, then `user`, `project`, and `local`), then by descending priority within that source, then by declaration order. A priority value never moves a hook ahead of a hook from an earlier source.

```toml
# meridian.toml (project) or ~/.meridian/config.toml (user)

[[hooks]]
name    = "notify-team"
builtin = "notify"
event   = "spawn.finished"
options = { channels = ["ntfy"] }

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
| `remote` | str | no | — | Deprecated: the removed `git-autosync` hook's shorthand; carried, unused, removed in the next minor release |
| `exclude` | array[str] | no | — | Deprecated: the removed `git-autosync` hook's file patterns; carried, unused, removed in the next minor release |
| `enabled` | bool | no | `true` | Set to `false` to disable without removing |
| `priority` | int | no | `0` | Higher values run first within the same source |
| `timeout_secs` | int | no | builtin default | Max seconds before hook is killed |
| `interval` | str | no | builtin default | Minimum time between runs, e.g. `30s`, `5m`, `2h` |
| `require_serial` | bool | no | `false` | Block concurrent hook executions |
| `when.status` | array[str] | no | — | On `spawn.finished`, fire only for `success`, `failure`, `cancelled`, or `timeout` (matching stored spawn statuses `succeeded`, `failed`, `cancelled`, and `timed_out`) |
| `when.agent` | str | no | — | Only fire for this agent profile (for notification events, the notifying session's agent) |
| `when.kind` | array[str] | no | — | On spawn events, only fire for these kinds: `child`, `primary`, `streaming`; notification events use the notifying spawn's kind when known; it cannot match work events |
| `options.channels` | array[str] | no | `["push", "email"]` | `notify` only: transports to deliver to |
| `options.priority` | int | no | notice's | `notify` only: 1–5 priority override |

`command` and `builtin` are mutually exclusive.

`failure_policy` is rejected; hook failures always continue.

Conditions that an event can never supply are config errors: `when.status` is
limited to `spawn.finished`, and work events cannot use `when.agent` or
`when.kind`. `when.status = ["skipped"]` is rejected too, because it never
matched. When an unevented builtin row expands across
its default events, Meridian keeps compatible registrations enabled and records
incompatible registrations as disabled overrides. Those disabled entries still replace
lower-precedence rows with the same name and event, so narrowing a higher row
cannot expose a lower layer's action. If no default can match, loading fails
and asks the row to set `event` explicitly. A runtime context that lacks or
does not match a configured condition skips the hook.

If you omit `name` for a builtin hook, Meridian names it from the options that
pick its target; `notify` rows are all `notify`.
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

Command hooks receive the event context as JSON on stdin (`schema_version` 4)
and as `MERIDIAN_HOOK_*`, `MERIDIAN_PROJECT_DIR`, `MERIDIAN_SPAWN_*`,
`MERIDIAN_ACTIVE_WORK_*`, `MERIDIAN_NOTIFICATION_*`, and `MERIDIAN_INBOX_MESSAGE_*`
environment variables. Each is set only when the event has that field:
`MERIDIAN_PROJECT_DIR`, for one, is unset for `inbox.received`.
Before adding the current payload, Meridian removes every payload variable
inherited from the caller, so an absent field cannot leak stale spawn, work, or
notification context. The resulting spawn and work variables describe only the
event's own context, not the caller's active spawn or work item. Hooks run even
in a directory with no Meridian runtime state; only `interval`-throttled hooks skip there.
`MERIDIAN_HOOKS_ENABLED=false` turns off all hook dispatch, including
notification delivery.

### JSON stdin payload

Every command hook receives one object like this on standard input:

```json
{
  "schema_version": 4,
  "event_name": "spawn.finished",
  "event_id": "2bd5e40f-8282-52a6-bfdb-28c456300e53",
  "timestamp": "2026-10-18T18:42:00+00:00",
  "project_root": "/workspace/project",
  "runtime_root": "/home/me/.meridian/projects/example",
  "spawn": {
    "id": "p12",
    "status": "success",
    "agent": "coder",
    "kind": "child",
    "model": "claude-haiku",
    "duration_secs": 4.2,
    "cost_usd": 0.01,
    "error": null
  },
  "work": {"id": "hook-event-names", "dir": "/workspace/project/.work"},
  "notification": null,
  "inbox_message": null
}
```

`spawn`, `work`, and `notification` are `null` when none of their fields is
known. `inbox_message` (`id`, `path`) is set only for `inbox.received`, where
`project_root` and `runtime_root` are `null`. The `spawn` object is present when any spawn field is known, even if
`spawn.id` is `null` (for example, a notification labeled with an agent but no
spawn). Individual unknown fields inside a present block are `null`.

## Builtin: `notify`

`notify` delivers a notification event through the transports configured in
`[notify]`. Options `channels` (default `["push", "email"]`, or backend names
such as `ntfy`) and `priority` (1–5) pick where and how loud.

**Default events:** `notify.your_turn`, `notify.cache_expiring`,
`notify.needs_permission`, `notify.manual`

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

Use the same row with `event = "notify.needs_permission"` to silence only
Claude's still-waiting permission notice.

A `command` hook on a notification event is an extra channel: it gets the
`notification` object on stdin and `MERIDIAN_NOTIFICATION_TITLE`, `_BODY`, and
`_PRIORITY` in its environment. See
[Routing and extending notifications](configuration.md#routing-and-extending-notifications)
for channel semantics and examples.

When notification emission finds invalid hook rows, it drops and names only
those rows, retains every valid row, and applies default-route overrides as
usual. `meridian hooks check` instead exits non-zero and names every invalid
row.

## Removed builtin: `git-autosync`

Git contexts now sync through the `meridian-sync` daemon, which watches each clone and
needs no hook row: see [Context Sync](commands.md#context-sync) and the `autosync` key in
[context configuration](configuration/context.md). A `[[hooks]] builtin = "git-autosync"`
row still loads, does nothing, and warns each time it would have run; delete it. `remote`,
`exclude` and `options.*` on such a row are ignored (the two top-level keys are removed with the stub). A row with `options.path` also made
local-only commits, which nothing replaces now. The stub is removed one minor release
after this one.

## MCP

Hook commands (`hooks list`, `hooks check`, `hooks run`) are CLI-only and not exposed via MCP. The MCP server exposes `extension_list_commands` and `extension_invoke` — hook state is accessible through extension commands if registered.

See [mcp-tools.md](mcp-tools.md) for the MCP tool reference.
