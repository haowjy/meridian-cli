# hooks/builtin/ — In-Process Built-In Hook Implementations

Built-ins run inside the Meridian process — no subprocess overhead — but they deliberately behave like external plugins: they import only from `meridian.plugin_api`, not from `meridian.lib.*`. This constraint makes them usable as reference implementations and keeps the plugin API surface honest.

## Mental Model

The dispatcher calls `_run_builtin(hook, context)` when it encounters a hook with a name matching a registered `BuiltinHook`. Built-ins receive a `PluginHookContext` (plugin API surface), not the internal `HookContext`. The bridge happens in `dispatch.py` via `_to_plugin_context()`.

## What Lives Here

- `base.py` — `BuiltinHook` protocol: `name`, `requirements`, `default_events`, `default_interval`, `check_requirements()`, `execute(hook, context, config)`
- `git_autosync.py` — `GIT_AUTOSYNC` singleton: commit-first merge-based sync (add → commit → fetch → merge → push)
- `autosync_store.py` — sole owner of the `.meridian/autosync/` file layout: conflict metadata JSON, sync state JSON, AGENTS.md notice injection/removal. Stdlib-only, zero meridian imports.

## GitAutosync Behavior

`GIT_AUTOSYNC` is the canonical example of a built-in hook. Key behaviors:

- **Commit-first**: local changes are committed before fetch/merge, so local work is never at risk from the incoming merge
- **Merge strategy**: fetches `origin`, then runs `git merge origin/<branch>` — not `pull --rebase`. Local commits are preserved as-is; remote changes are merged in on top.
- **Local-only mode**: when no remote is configured, stops after commit (skips fetch/merge/push)
- **Conflict policy**: `"abort"` (default) — on merge conflict, runs `git merge --abort` (local-wins), records conflict metadata via `autosync_store`, and appends a notice block to `AGENTS.md` so agents working in the repo see it
- **Cannot push after conflict**: once a conflict is recorded, the clone stays behind `origin` until the conflict is manually resolved and marked resolved. Subsequent syncs skip with `skip_reason="conflict_detected"`.
- **Excluded file stashing**: files matching `exclude` patterns are stashed before merge and popped after, preventing dirty working-tree state from blocking the merge
- **Divergence fallback**: `rev-list --left-right --count` is tried first; falls back to `git log --oneline` pair if that fails; returns `skip_reason="divergence_check_failed"` if both fail
- **Pre-existing conflict/rebase recovery**: if `MERGE_HEAD` or a rebase directory is detected at sync start, the hook aborts the in-progress operation (when `conflict_policy="abort"`) before proceeding
- **Default ignores**: `.git`, `**/.git`, `.meridian/autosync/` are written into `.git/info/exclude` on each run so autosync artifacts are never staged
- **Always returns `HookResult`**: never raises. Failures emit `outcome="skipped"`, not exceptions — the dispatcher is fail-open for `observe` and `post` events

## Import Boundaries

`git_autosync.py` imports from:
- `meridian.plugin_api` — the stable external surface
- `meridian.lib.hooks.builtin.autosync_store` — the artifact store companion (stdlib-only)

`autosync_store.py` imports from stdlib only. This pairing is intentional: together they could be extracted into a standalone plugin package without pulling in `meridian.lib.*`.

If `git_autosync.py` ever needs something else from `meridian.lib.*`, extend `meridian.plugin_api` rather than breaking the boundary.

## Adding a Built-In

1. Implement the `BuiltinHook` protocol in a new module here
2. **Import only from `meridian.plugin_api`** — zero `meridian.lib.*` imports
3. Register the instance in `lib/hooks/dispatch.py`'s built-in registry (`BUILTIN_HOOKS`)

If you need something from `meridian.lib.*`, that's a signal to extend `meridian.plugin_api` instead.

## Related

→ [../.context/CONTEXT.md](../.context/CONTEXT.md) — plugin API boundary rule, event classes, failure behavior, execution order
→ `../../plugin_api/` — the stable surface all built-ins must import from
