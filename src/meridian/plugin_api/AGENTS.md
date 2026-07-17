# plugin_api/ — Stable External Plugin Surface

The only surface external plugins (and built-in hooks) may import from. Versioned at `1.0.0` — breaking changes require a version bump and migration path. Internal `meridian.lib.*` is not stable; this is.

## Mental Model

Built-in hooks in `lib/hooks/builtin/` must import only from here — not from `meridian.lib.*`. This constraint enforces the plugin API as a real boundary. If a built-in needs something not in this package, extend the API rather than breaking the boundary.

External plugins follow the same rule: import `Hook`, `HookContext`, etc. from `meridian.plugin_api`, not from internal modules that may change without notice.

## What's Stable Here

- `types.py` — `Hook`, `HookContext`, `HookResult`, `HookEventName`, `HookOutcome`, `FailurePolicy` — the hook contract types
- `state.py` — `get_project_home()`, `get_user_home()` — state root access
- `fs.py` — `file_lock()`, `atomic_write_text()` — cross-platform locking and atomic
  replacement for plugins
- `git.py` — `resolve_clone_path()` — git clone path helpers
- `config.py` — `get_user_config()` — user config access

## Import Pattern

```python
from meridian.plugin_api import Hook, HookContext      # stable, re-exported from __init__
from meridian.plugin_api.fs import atomic_write_text, file_lock  # stable submodule imports
```

Submodule utilities (`fs`, `git`, `config`) are NOT re-exported from `__init__.py`. Import them directly from their submodule.

## Key Rules

- `HookContext.to_env()` and `HookContext.to_json()` are the canonical serialization paths. External hooks receive both. Use them; don't build the serialization manually.
- Breaking this API requires a version bump. Additions are fine; removals or signature changes are breaking.
- Stable wrappers may delegate to dependency-neutral `meridian.lib.platform` primitives.
  They must not depend on `meridian.lib.state` or other policy/state internals.

## Related

- `../lib/hooks/builtin/` — built-in hooks that use this API as their only import surface
- `../lib/hooks/` — internal hook dispatch that bridges `HookContext` to `PluginHookContext`
