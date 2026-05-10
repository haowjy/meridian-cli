# Plugin API

`meridian.plugin_api` is the stable public contract for hook and plugin authors. It version-pins at `1.0.0` and exposes only the symbols intended for external use.

Install meridian-cli and import from this package directly — do not import from internal submodules.

```python
from meridian.plugin_api import Hook, HookContext, HookResult, HookOutcome
```

## Version

```python
import meridian.plugin_api as api
print(api.__version__)  # "1.0.0"
```

## Hook Types

| Export | Purpose |
| ------ | ------- |
| `Hook` | Resolved hook record — `name`, `event`, `builtin`, `command`, `options`, `remote`, `enabled`, `priority`, `failure_policy`, `require_serial`, `exclude` |
| `HookContext` | Runtime context passed to a hook at execution time — spawn state, work item, event name |
| `HookResult` | Result for one hook execution — `hook_name`, `event`, `outcome`, `success`, `skipped`, `skip_reason`, `error`, `exit_code`, `duration_ms`, `stdout`, `stderr` |
| `HookOutcome` | `"success"` \| `"failure"` \| `"timeout"` \| `"skipped"` |
| `HookEventName` | Valid event name literal — `spawn.created`, `spawn.running`, `spawn.start`, `spawn.finalized`, `work.start`, `work.started`, `work.done` |
| `FailurePolicy` | `"fail"` \| `"warn"` \| `"ignore"` |

### `HookContext` fields

| Field | Type | Present for |
| ----- | ---- | ----------- |
| `event_name` | `HookEventName` | all events |
| `event_id` | `UUID` | all events |
| `timestamp` | `str` (ISO 8601) | all events |
| `project_root` | `str` | all events |
| `runtime_root` | `str` | all events |
| `schema_version` | `int` | all events |
| `spawn_id` | `str \| None` | spawn events |
| `spawn_status` | `str \| None` | `spawn.finalized` |
| `spawn_agent` | `str \| None` | spawn events |
| `spawn_model` | `str \| None` | spawn events |
| `spawn_duration_secs` | `float \| None` | `spawn.finalized` |
| `spawn_cost_usd` | `float \| None` | `spawn.finalized` |
| `spawn_error` | `str \| None` | `spawn.finalized` (on failure) |
| `work_id` | `str \| None` | work events |
| `work_dir` | `str \| None` | work events |

`HookContext.to_env()` returns a `dict[str, str]` of `MERIDIAN_HOOK_*` environment variables. Shell-command hooks receive these automatically. `HookContext.to_json()` returns the context as a JSON string (passed via stdin for stdin-transport hooks).

## State Helpers

```python
from meridian.plugin_api import get_project_home, get_user_home

# Resolve the per-machine state root for the current project
runtime_root = get_project_home()

# Resolve the user-level Meridian state root (~/.meridian or %LOCALAPPDATA%\meridian)
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
name    = "notify"
command = "notify-send 'spawn done'"
event   = "spawn.finalized"
```

**Shell behavior:** The `command` string is passed to the platform default shell — `sh -c` on POSIX and `cmd.exe /c` on Windows. Inline bash syntax will not work on Windows. For cross-platform hooks, point `command` at a script file (`.sh`, `.ps1`, or `.bat`) rather than embedding shell syntax inline.

A Python builtin-style hook using the plugin API (for hooks distributed as packages):

```python
from meridian.plugin_api import HookContext, HookResult, HookOutcome

def run(ctx: HookContext) -> HookResult:
    print(f"Hook fired for event: {ctx.event}")
    return HookResult(outcome=HookOutcome.SUCCESS)
```

See [hooks.md](hooks.md) for hook configuration schema and available builtins.
