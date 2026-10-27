# hooks/builtin/ — In-Process Built-In Hooks

Built-ins run inside the Meridian process. The dispatcher calls `_run_builtin()` for a hook
whose `builtin` field (not its `name`) names a registered `BuiltinHook`; a command hook
named `notify` is just a command.

## Rules

- `base.py` `BuiltinHook` is the only owner of a built-in's metadata (events, interval,
  `options_model`, `identity_options`).
- **Adding one**: implement the protocol in a new module, register it in `BUILTIN_HOOKS`
  (`builtin/__init__.py`); config validation and the dispatcher both read it.
- **Import general capabilities only from `meridian.plugin_api`.** If you need something
  else from `meridian.lib.*`, extend the plugin API, unless it is a tightly coupled
  companion: `autosync_store` for `git_autosync`; `lib/notify` transports and
  `lib.config.loader` for `notify`, imported inside `execute` so config loading (which
  imports every built-in for its options model) stays free of the notify stack.
- `autosync_store.py` is the sole owner of the `.meridian/autosync/` layout and its
  sync-root transaction lock. It uses the reentrant platform lock directly because a whole
  autosync transaction is stronger than the plugin lock API; lock rules in
  [../../state/AGENTS.md](../../state/AGENTS.md).
- **Built-ins always return `HookResult`, never raise**; failures are `outcome="skipped"`.
  `HookOutcome` has no `conflict_detected`: a conflict is `skipped` with
  `skip_reason="conflict_detected"`.
- Use `hook_event=`, not `event=`, in structlog calls (`event` is reserved).

[.context/CONTEXT.md](.context/CONTEXT.md) — git-autosync behavior and ordering
contracts, notify delivery.
