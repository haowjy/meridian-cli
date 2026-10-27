# plugin_api/ — Stable External Plugin Surface

The only surface external plugins may import from. Breaking
changes follow the project's compatibility and migration policy; this package
does not carry an independent version. Internal `meridian.lib.*` is not stable;
this is.

## Boundary

External plugins import `Hook`, `HookContext`, etc. from `meridian.plugin_api`, never
from `meridian.lib.*`. Built-in hooks follow the same rule for general capabilities; if
one needs something this package lacks, extend the API. A built-in's tightly coupled
companion (`lib/hooks/builtin/AGENTS.md`) may import `lib` directly, and such companions
never belong here.

## What's Stable Here

- `types.py` — `Hook`, `HookContext`, `HookResult`, `HookEventName`, `HookOutcome`, `SpawnKind`, `SpawnStatus`, `Notification`, `NOTIFICATION_EVENTS` — the hook contract types. Builtin implementations may set `HookResult.details` to JSON-serializable data and document where their keys surface. Core, state, and `lib/hooks` use these exact closed vocabularies and payload classes rather than defining parallel copies.
- `state.py`, `fs.py`, `git.py`, `config.py` — state-root access, locking/atomic writes,
  clone paths, user config. Read user config through `get_user_config()` so `--config`
  and `MERIDIAN_CONFIG` apply; never read `~/.meridian/config.toml` directly.

## Import Pattern

```python
from meridian.plugin_api import Hook, HookContext      # stable, re-exported from __init__
from meridian.plugin_api.fs import atomic_write_text, file_lock  # stable submodule imports
```

Submodule utilities (`fs`, `git`, `config`) are NOT re-exported from `__init__.py`. Import them directly from their submodule.

## Key Rules

- Additions are fine; removals or signature changes follow the compatibility
  policy in `docs/releasing.md`.
- Wrappers may delegate to dependency-neutral primitives (`lib.platform` locking and
  atomic writes, `lib.state.user_paths`, `lib.config.project_root`). They must not pull in
  the state stores or other policy internals.
