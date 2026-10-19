# hooks/builtin/ — In-Process Built-In Hook Implementations

Built-ins run inside the Meridian process with no subprocess overhead. They use the plugin API for general capabilities; tightly coupled companion modules may use internal primitives when the plugin API intentionally exposes a weaker contract.

## Mental Model

The dispatcher calls `_run_builtin(hook, context)` for a hook whose `builtin` field names a registered `BuiltinHook` (`hook.builtin`, not the hook's `name`: a command hook named `notify` is just a command). Built-ins receive the plugin API's `HookContext` (the dispatcher uses that type itself) and a plugin `Hook` built by `_to_plugin_hook()`.

## What Lives Here

- `base.py` — `BuiltinHook` protocol: `name`, `requirements`, `default_events`, `supported_events` (`None` means any), `default_interval`, `options_model` (Pydantic; `schema.py` validates a row's `options` with it), `identity_options` (options that name an unnamed row), `check_requirements()`, `execute(context, config)`. The builtin object is the only owner of this metadata.
- `git_autosync.py` — `GIT_AUTOSYNC` singleton: commit-first merge-based sync (add → commit → fetch → merge → push)
- `notify.py` — `NOTIFY`: delivers a notification event's `HookContext.notification` through `lib/notify`'s `send()` to the channels its row's `options.channels` names (unset: `send`'s defaults), with an optional `options.priority`. `HookResult.details["transports"]` names successful backends, partial delivery goes in `warning`, failure names each failed transport in `error`, and a route that delivered nothing is skipped (`disabled`). Unnamed `notify` rows are all named `notify` (`identity_options = ()`).
- `autosync_store.py` — sole owner of the `.meridian/autosync/` file layout and its sync-root transaction lock: conflict metadata JSON and sync state JSON.

## GitAutosync Behavior

`GIT_AUTOSYNC` is the canonical example of a built-in hook. Key behaviors:

- **Commit-first**: local changes are committed before fetch/merge, so local work is never at risk from the incoming merge
- **Merge strategy**: fetches `origin`, then runs `git merge origin/<branch>` — not `pull --rebase`. Local commits are preserved as-is; remote changes are merged in on top.
- **Local-only mode**: when no remote is configured, stops after commit (skips fetch/merge/push)
- **Conflict policy**: a merge conflict created by autosync is recorded through `autosync_store` and aborted. `"abort"` (default) and `"leave"` govern only a merge or rebase already in progress when the run starts: abort and continue, or leave it in place and skip.
- **Conflict retries**: a later event retries the commit/fetch/merge workflow. If that merge conflicts while any unresolved conflict record exists for the sync root, it aborts and returns `skip_reason="conflict_detected"` without adding another record.
- **Excluded file stashing**: files matching `exclude` patterns are stashed before merge and popped after, preventing dirty working-tree state from blocking the merge
- **Divergence fallback**: `rev-list --left-right --count` is tried first; falls back to `git log --oneline` pair if that fails; returns `skip_reason="divergence_check_failed"` if both fail
- **Default ignores**: `.git` and `**/.git` are written into `.git/info/exclude` on each run
- **Always returns `HookResult`**: never raises. Failures emit `outcome="skipped"`, not exceptions — the dispatcher is fail-open for `observe` and `post` events

## Import Boundaries

`git_autosync.py` imports from:
- `meridian.plugin_api` — the stable external surface
- `meridian.lib.hooks.builtin.autosync_store` — the artifact store and transaction companion

`notify.py` imports `meridian.plugin_api` at module level and, inside `execute`, its companion `lib/notify` transports and `lib.config.loader`. The transports are Meridian's own delivery policy, not a plugin capability, so they are not in the plugin API. The lazy import keeps config loading (which imports every builtin for its options model) free of the notify stack.

`autosync_store.py` directly imports the reentrant platform lock because a complete autosync transaction is stronger than the intentionally non-reentrant plugin lock API. The store owns that exception and the canonical lock identity.

If `git_autosync.py` ever needs something else from `meridian.lib.*`, extend `meridian.plugin_api` rather than breaking the boundary.

## Adding a Built-In

1. Implement the `BuiltinHook` protocol in a new module here, including its `options_model`
2. Import general capabilities only from `meridian.plugin_api`; keep any tightly coupled companion import explicit and narrowly scoped
3. Register the instance in `BUILTIN_HOOKS` (`builtin/__init__.py`); config validation and the dispatcher both read it

If you need something from `meridian.lib.*`, that's a signal to extend `meridian.plugin_api` instead.

## Related

→ [../.context/CONTEXT.md](../.context/CONTEXT.md) — plugin API boundary rule, per-event facts, failure behavior, execution order
→ `../../plugin_api/` — the stable surface all built-ins must import from
