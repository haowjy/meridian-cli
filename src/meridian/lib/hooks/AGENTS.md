# lib/hooks — Lifecycle Event Callbacks

Hooks fire at spawn, work and notification events for side effects: syncing, notifying, triggering pipelines. They do not modify agent behavior — they observe it.

Notification events (`session.responded`, `session.cache_expiring`, `notify.manual`) carry `HookContext.notification`. Delivering them is itself a hook: the built-in `notify` routes them to transports, and `config.py` supplies a `notify` row per notification event as the lowest layer (source `default`). An unnamed `builtin = "notify"` row is named `notify`, so a row for an event, even disabled, replaces that event's default by the ordinary `(name, event)` override. Command hooks on these events are the extension channels. A notification's emitter (`lib/notify/emit.py`) reports route failures itself, so the dispatcher logs them at info.

If configured hook rows do not load, notification emission logs the error,
then dispatches against only that default layer. Direct hook inspection
still raises the config error; the fallback exists only so idle notifications
survive an unrelated broken hook row.

## Mental Model

Users configure hooks in TOML (project or user config). At each lifecycle event (`spawn.created`, `spawn.finalized`, `work.done`, etc.), `HookDispatcher.fire()` loads the hook list for that event, filters by conditions, checks interval throttling, and executes each hook in order. Hooks are either built-in (in-process) or external (subprocess).

The dispatcher is **fail-open** for `observe` and `post` event classes. A failing hook logs a warning and continues — it never stops subsequent hooks or the lifecycle event itself. Only `gate` class hooks can block (none currently registered).

Config layers in ascending precedence:
```
default (Meridian's notify routes) < user (~/.meridian/config.toml) < project (meridian.toml) < local (meridian.local.toml)
```
Override key is `(name, event)` — a higher-priority row with the same key replaces the lower-priority one entirely.

## Key Rules

- **Fire `HookDispatcher.fire(context)` — don't instantiate the registry or runner directly.** `HookDispatcher.from_application_context(ctx)` is the preferred constructor.
- **Built-in hooks take general capabilities from `meridian.plugin_api`.** If a built-in needs a general capability not in the API, extend the API. The only `meridian.lib.*` imports are a built-in's tightly coupled companion (`autosync_store`; `notify`'s `lib/notify` transports, lazily); see `builtin/AGENTS.md`.
- **A builtin is one object owning its metadata**: default events, interval, Pydantic `options_model` (validated by `schema.py` when the row loads) and `identity_options` (which options name an unnamed row). Adding one = a module in `builtin/` + an entry in `BUILTIN_HOOKS`.
- **`HookContext`, `HookResult` and `HookEventName` are the plugin API's types**, re-exported from `types.py`. Built-ins receive exactly what the dispatcher builds; never fork a lib copy.
- **Hooks fire without a runtime root** (an ad-hoc directory), `hooks run` included: only `interval` hooks skip, with nowhere to keep state. A built-in that needs the runtime root checks `context.runtime_root` once in `execute`.
- **`HookDispatcher(env=...)`** is the environment hooks run in and whose `MERIDIAN_HOOKS_ENABLED` gates them; it defaults to `os.environ`.
- **`HookContext.to_env()` and `HookContext.to_json()` are the only correct serialization paths.** External subprocess hooks receive both env vars and JSON stdin. `None` values are omitted — no empty-string env vars.
- **Hook execution order within an event:** source rank (default → user → project → local) → negative priority → declaration order. This is stable and deterministic. A configured builtin row ranks by the file it came from.
- **Hook rows validate through `schema.py` models.** `HookConfigSection.model_validate` is the production path; single-field constraints (event, interval, timeout, failure policy, status) live on `HookSpec` / `HookWhenSpec` fields. Hand checks after the model are only for multi-field rules (command vs builtin, conditional name/event, builtin defaults and options). Don't add per-field checks outside the model, and don't warn about unknown keys here: config loading already warns once.
- **`when.status` and `when.agent` conditions** are checked before interval throttling.

## Entry Points

- `dispatch.py` — `HookDispatcher.fire(context)`, `HookDispatcher.from_application_context(ctx)`
- `registry.py` — `HookRegistry` (loads config, serves event-scoped hook lists)
- `config.py` — `load_hooks_config(project_root, ...)` with source layering
- `schema.py` — `HookSpec` parsing, validation, and runtime `Hook` construction
- `types.py` — `Hook`, `HookContext`, `HookResult`, `HookEventName`
- `builtin/` — built-in hook implementations (`git_autosync.py`, and `notify.py` for notification delivery)

## Anti-Patterns

- Don't import from submodules directly — `from meridian.lib.hooks import HookDispatcher` works via lazy `__getattr__` in `__init__.py`. This avoids circular import issues at startup.
- Don't set `failure_policy = "fail"` on observe-class hooks without deliberate intent — fail-open is the correct default for observation.
- Don't use `meridian.local.toml` in version control — it's for machine-specific overrides.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — config layering, event classes, failure behavior, plugin API boundary, context transport schema
→ KB: `$MERIDIAN_CONTEXT_KB_DIR/concepts/hooks-and-plugins.md` (see `meridian context kb`)
