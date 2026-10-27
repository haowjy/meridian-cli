# lib/hooks — Lifecycle Event Callbacks

Hooks fire at spawn, work and notification events for side effects (sync, notify,
pipelines). They observe; they don't change agent behavior. The dispatcher is
**fail-open**: a failing hook logs a warning and never stops later hooks or the lifecycle
event.

Rows layer default < user < project < local; override key is `(name, event)`, and a
higher-priority row replaces the lower one entirely. Order within an event: source rank →
negative priority → declaration order.

## Rules

- **Fire `HookDispatcher.fire(context)`**, built via `from_application_context(ctx)`; don't
  instantiate the registry or runner directly. Import from `meridian.lib.hooks` (lazy
  `__getattr__`), not submodules, to avoid startup import cycles.
- **Built-ins take general capabilities from `meridian.plugin_api`**; the exceptions are a
  built-in's tightly coupled companions, see [builtin/AGENTS.md](builtin/AGENTS.md).
- **`HookContext`, `HookResult`, `HookEventName` are the plugin API's types**, re-exported
  from `types.py`; never fork a lib copy. `to_env()` and `to_json()` are the only
  serialization paths (`None` omitted, no empty-string env vars).
- **Hook rows validate only through `config.py` and `schema.py`.** Single-field constraints
  live on `HookSpec` / `HookWhenSpec` fields; hand checks are only for multi-field rules.
  Operational config carries hook rows unvalidated.
- **`EVENTS` owns each event's timeout and available `when` fields.** Conditions are checked
  before interval throttling.
- **Invalid rows**: runtime emission drops and names only the invalid rows and layers the
  rest over defaults; direct inspection (`hooks list/check`) raises all row errors.
- **Notification delivery is itself a hook**: `config.py` supplies a `notify` row per
  notification event as the lowest layer, so any row for that event, even disabled,
  replaces it.
- **Hooks fire without a runtime root** (ad-hoc directory): only `interval` hooks skip; a
  built-in that needs it checks `context.runtime_root` once in `execute`.
- **`meridian hooks list/check/run` resolve the project from cwd, never an inherited
  `MERIDIAN_PROJECT_DIR`** (`ignore_env=True` in `cli/hooks_commands.py`): a spawn's
  environment must not make `hooks run` act on another project's config.
- `interval.py` takes a non-reentrant `lock_file`; lock rules in
  [../state/AGENTS.md](../state/AGENTS.md).

[.context/CONTEXT.md](.context/CONTEXT.md) — config layering, event facts, context
transport schema, `when` conditions.
