# plugin_api/

Stable public API for hooks and plugins. Version-pinned (`__version__ = "1.0.0"`).
Breaking changes require a version bump. External plugin authors import from
here, not from `meridian.lib.*`.

## Entry Points

- `types.py` — `Hook`, `HookContext`, `HookResult`, `HookEventName`,
  `HookOutcome`, `FailurePolicy` — the hook contract types
- `state.py` — `get_project_home()`, `get_user_home()` — state root access
- `fs.py` — `file_lock()` — cross-platform file locking for plugins
- `git.py` — `resolve_clone_path()` — git clone path helpers
- `config.py` — `get_user_config()` — user config access

## Stability Contract

Submodule re-exports (`fs`, `git`, `config`) are **not** re-exported from
`plugin_api/__init__.py`. Import them directly:

```python
from meridian.plugin_api import Hook, HookContext      # stable, re-exported
from meridian.plugin_api.fs import file_lock           # stable, direct import
```

`HookContext.to_env()` and `HookContext.to_json()` are the canonical
serialization paths for shell and stdin hook transport respectively.

## Related

- `../lib/hooks/` — internal hook dispatch (consumes plugin_api types)
- `../lib/extensions/` — extension system (separate concern from hooks)
