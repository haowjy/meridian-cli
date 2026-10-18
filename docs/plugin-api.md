# Plugin API

`meridian.plugin_api` is the stable public contract for hook and plugin authors. It version-pins at `3.0.0` and exposes only the symbols intended for external use.

Install meridian-cli and import from this package directly — do not import from internal submodules.

```python
from meridian.plugin_api import Hook, HookContext, HookResult, HookOutcome, SpawnStatus
```

## Version

```python
import meridian.plugin_api as api
print(api.__version__)  # "3.0.0"
```

## Hook Types

| Export | Purpose |
| ------ | ------- |
| `Hook` | Resolved hook record — `name`, `event`, `builtin`, `command`, `options`, `remote`, `enabled`, `priority`, `require_serial`, `exclude` |
| `HookContext` | Runtime context passed to a hook at execution time — spawn state, work item, event name |
| `HookResult` | Result for one hook execution — `hook_name`, `event`, `outcome`, `success`, `skipped`, `skip_reason`, `warning`, `error`, `exit_code`, `duration_ms`, `stdout`, `stderr`, `details` |
| `HookOutcome` | `"success"` \| `"failure"` \| `"timeout"` \| `"skipped"` |
| `HookEventName` | Valid event name literal — `spawn.created`, `spawn.started`, `spawn.finished`, `work.created`, `work.activated`, `work.done`, `notify.your_turn`, `notify.cache_expiring`, `notify.needs_permission`, `notify.manual` |
| `SpawnKind` | Spawn kind literal — `"child"` \| `"primary"` \| `"streaming"` |
| `SpawnStatus` | Hook terminal status literal — `"success"` \| `"failure"` \| `"cancelled"` \| `"timeout"` |
| `NOTIFICATION_EVENTS` | Every notification event, each carrying `HookContext.notification` |
| `Notification` | One notice — `title`, `body`, `priority` (1–5), `email`, `label` (`tmux_session`, `project`, `work_id`, `agent`, `harness`, `headline`), `attachments` (absolute file paths) |

`HookResult.details` is a mapping that builtin hook implementations may set with
JSON-serializable structured data. Builtins document their keys in
[hooks.md](hooks.md); consumers must not assume unrelated hooks share them.
Today only the `notify` builtin sets a key: `transports` is projected into
`meridian notify` text and JSON reports. `hooks run --json` does not expose
arbitrary `details`.

### `HookContext` fields

| Field | Type | Present for |
| ----- | ---- | ----------- |
| `event_name` | `HookEventName` | all events |
| `event_id` | `UUID` | all events |
| `timestamp` | `str` (ISO 8601) | all events |
| `project_root` | `str` | all events |
| `runtime_root` | `str \| None` | all events; `None` in a directory with no Meridian runtime state |
| `schema_version` | `int` (currently `3`) | all events |
| `spawn_id` | `str \| None` | spawn events; notification events when the notifying spawn is known |
| `spawn_status` | `SpawnStatus \| None` | `spawn.finished`; one of `success`, `failure`, `cancelled`, or `timeout` |
| `spawn_agent` | `str \| None` | spawn events; notification events when known |
| `spawn_kind` | `SpawnKind \| None` | spawn events; notification events when the notifying spawn is known |
| `spawn_model` | `str \| None` | spawn events |
| `spawn_duration_secs` | `float \| None` | `spawn.finished` |
| `spawn_cost_usd` | `float \| None` | `spawn.finished` |
| `spawn_error` | `str \| None` | `spawn.finished` (on failure) |
| `work_id` | `str \| None` | work events; spawn events when the spawn has work; notification events when the session has work |
| `work_dir` | `str \| None` | work events |
| `notification` | `Notification \| None` | notification events |

`HookContext.to_env()` returns a `dict[str, str]` of `MERIDIAN_HOOK_*` and related environment variables, including `MERIDIAN_SPAWN_KIND` when known (notification events add `MERIDIAN_NOTIFICATION_TITLE`, `MERIDIAN_NOTIFICATION_BODY`, `MERIDIAN_NOTIFICATION_PRIORITY`). Shell-command hooks receive these automatically. `HookContext.to_json()` returns the context as a JSON string (passed via stdin for stdin-transport hooks); its nested payload shape and null rules are documented in [Hooks: JSON stdin payload](hooks.md#json-stdin-payload).

## State Helpers

```python
from meridian.plugin_api import get_project_home, get_user_home

# Resolve the per-machine state root for the current project
runtime_root = get_project_home()

# Resolve the user-level Meridian state root (~/.meridian by default)
user_root = get_user_home()
```

## Submodule Utilities

The following helpers are available via submodule imports. They are not re-exported from the top-level `meridian.plugin_api` package — import them from the submodule directly.

### Git Helpers

Used by `git-autosync` and useful for any hook that operates on a remote repo.

```python
from meridian.plugin_api.git import generate_repo_slug, normalize_repo_url, resolve_clone_path

url = "git@github.com:team/docs.git"
slug = generate_repo_slug(url)           # "github.com-team-docs"
normalized = normalize_repo_url(url)     # canonical form
clone_path = resolve_clone_path(url)     # path where the repo would be cloned
```

### Config Helpers

```python
from meridian.plugin_api.config import get_user_config, get_git_overrides

config = get_user_config()          # parsed user config dict
overrides = get_git_overrides()     # config values from git config
```

### File Locking

```python
from meridian.plugin_api.fs import file_lock

with file_lock("/path/to/lockfile"):
    # exclusive section
    ...
```

Cross-process file lock. Use this in hooks that must not run concurrently across spawns.

## Writing a Hook

A minimal custom hook registered via `command`:

```toml
# meridian.toml
[[hooks]]
name    = "spawn-done-alert"
command = "notify-send 'spawn done'"
event   = "spawn.finished"
```

**Shell behavior:** The `command` string is passed to `sh -c`. For non-trivial
shell logic, point `command` at a script file rather than embedding it inline.

A Python builtin-style hook using the plugin API (for hooks distributed as packages):

```python
from meridian.plugin_api import HookContext, HookResult

def run(ctx: HookContext) -> HookResult:
    print(f"Hook fired for event: {ctx.event_name}")
    return HookResult(
        hook_name="example",
        event=ctx.event_name,
        outcome="success",
        success=True,
    )
```

See [hooks.md](hooks.md) for hook configuration schema and available builtins.
