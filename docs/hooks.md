# Hooks

Hooks let you run commands or builtins automatically when Meridian events fire — syncing a remote repo, running checks after a spawn finishes, or any custom script you want wired to the agent lifecycle.

## Commands

| Command | Description |
| ------- | ----------- |
| `meridian hooks list` | Show all registered hooks and their resolved configuration |
| `meridian hooks check` | Validate hook config and exit non-zero on errors |
| `meridian hooks run NAME` | Execute a hook manually, bypassing interval throttling |
| `meridian hooks run NAME --event EVENT` | Execute with a specific event context |

```bash
# See all hooks, including builtins and per-source registration
meridian hooks list

# Validate config before committing changes
meridian hooks check

# Manually trigger a named autosync hook
meridian hooks run sync-notes

# Trigger with a specific event context
meridian hooks run sync-notes --event work.done
```

`hooks run` bypasses interval throttling. Use it to test hooks or force a sync outside the normal lifecycle.

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

Event class affects default timeout and failure policy: `observe` events default to 30s / `warn`; `post` events default to 60s / `warn`.

A single hook row can register for multiple events. When a builtin supplies default events and no `event` field is set, one hook registration is created per default event. Setting `event` explicitly overrides this and registers for only that one event.

## Configuring Hooks

Hooks live in any Meridian config file. Precedence is: `builtin < context < user < project < local`.

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
| `command` | str \| array[str] | one of `builtin`/`command` | — | Shell command string or argv array (see Shell Behavior) |
| `event` | str | yes (unless builtin supplies defaults) | builtin default | Event that triggers the hook |
| `remote` | str | required by `git-autosync` | — | Git remote URL for hooks that operate on a remote repo |
| `enabled` | bool | no | `true` | Set to `false` to disable without removing |
| `priority` | int | no | `0` | Lower values run first |
| `failure_policy` | str | no | builtin default | `fail` \| `warn` \| `ignore` |
| `timeout_secs` | int | no | builtin default | Max seconds before hook is killed |
| `interval` | str | no | builtin default | Minimum time between runs, e.g. `30s`, `5m`, `2h` |
| `require_serial` | bool | no | `false` | Block concurrent hook executions |
| `when.status` | array[str] | no | — | Only fire when spawn exits with one of these statuses |
| `when.agent` | str | no | — | Only fire for this agent profile |
| `exclude` | array[str] | no | — | Skip for these agent profiles |
| `options.conflict_policy` | str | no | `"leave"` | `git-autosync` only: `"leave"` keeps rebase conflicts for review; `"abort"` restores old abort behavior |

`command` and `builtin` are mutually exclusive.

If you omit `name` for a builtin hook, Meridian may synthesize a stable name from the builtin identity (for example remote/options). That keeps distinct unnamed builtin rows from overriding each other. For predictable `hooks run NAME` usage, set `name` explicitly.

### Shell Behavior

Hook `command` accepts either a shell string or an argv array:

- **String form** runs via the platform default shell: `sh -c` on POSIX (Linux/macOS) and `cmd.exe /c` on Windows. This is standard Python `subprocess(shell=True)` behavior.
- **Array form** runs with `shell=False`: the first element is the executable and the rest are arguments. Use this when you need injection hardening or explicit interpreter selection.

Inline commands that rely on bash syntax — `&&`, `||`, `$()`, `[[`, or POSIX-only tools — will fail on Windows because `cmd.exe` does not understand them.

For hooks that must run on both platforms, prefer argv arrays that invoke an interpreter explicitly:

```toml
# POSIX — explicit bash
[[hooks]]
name    = "check"
command = ["bash", "scripts/check.sh"]
event   = "spawn.finalized"

# Windows — explicit PowerShell
[[hooks]]
name    = "check-win"
command = ["powershell", "-File", "scripts/check.ps1"]
event   = "spawn.finalized"
```

String form with script files still works when the platform shell can execute them directly:

```toml
# POSIX — shell script (string form)
[[hooks]]
name    = "check"
command = "./scripts/check.sh"
event   = "spawn.finalized"

# Windows — batch file or PowerShell (string form)
[[hooks]]
name    = "check"
command = "powershell -File scripts/check.ps1"
event   = "spawn.finalized"
```

If you only target one platform, inline commands are fine. If portability matters, point `command` at a script file rather than embedding shell syntax in TOML.

### `repo` → `remote` Migration

`repo` is accepted as a deprecated alias for `remote`. Meridian emits a warning when it sees `repo`. Update your config:

```toml
# Before (deprecated)
[[hooks]]
builtin = "git-autosync"
repo = "git@github.com:team/docs.git"

# After
[[hooks]]
builtin = "git-autosync"
remote = "git@github.com:team/docs.git"
```

## Builtin: `git-autosync`

`git-autosync` keeps a remote Git repo in sync with local changes. On each trigger it:

1. Stages and commits any uncommitted changes in the target repo
2. Fetches upstream
3. Rebases when behind remote
4. Pushes when ahead or after a local commit

By default, if `git pull --rebase` encounters a conflict, the rebase is left in place
so agents or humans can inspect and resolve the conflict markers. Future autosync runs
detect the existing rebase state and skip all git operations until the conflict is resolved.

Set `conflict_policy = "abort"` in options to restore the previous behavior of aborting

```toml
# Restore old abort-on-conflict behavior
[[hooks]]
builtin = "git-autosync"
remote  = "git@github.com:team/docs.git"
[hooks.options]
conflict_policy = "abort"
```
the rebase on conflict.

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
