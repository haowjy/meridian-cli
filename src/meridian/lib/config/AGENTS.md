# lib/config/

Owns project-wide operational configuration: `MeridianConfig`, the TOML+env
precedence chain, and config schema introspection. Loads at startup and applies
broadly. This is not the same system as per-spawn runtime overrides.

## Mental Model: Two Config Systems — Don't Conflate

**`MeridianConfig`** — persistent, project-wide settings: retry policy, timeouts,
output verbosity, state retention, default model/harness. Loaded from TOML files.
Lives in this module.

**`RuntimeOverrides`** — per-spawn behavioral choices: model, harness, effort, approval,
sandbox, autocompact, timeout. Assembled fresh per spawn from a 7-tier precedence ladder.
Lives in `../core/overrides.py`.

These answer different questions. "What are the project's configured defaults?" →
`MeridianConfig`. "What should this specific spawn do?" → `RuntimeOverrides`. Never
move per-spawn policy into `MeridianConfig`.

## Config Precedence Chain

```
hardcoded defaults
  < user config (~/.meridian/config.toml)
    < project config (meridian.toml)
      < local config (meridian.local.toml)
        < environment variables
```

First-wins. Env vars beat local beats project beats user. `load_config()` is the
public entry point.

## Key Rules

**Use stdlib `logging.getLogger(__name__)`, not structlog.** The launch diagnostic
boundary (`capture_library_diagnostics()`) captures stdlib warnings during spawn.
Structlog bypasses it and leaks to stderr. This rule applies to both `lib/config/`
and `lib/catalog/`.

**`_SETTINGS_CONTEXT` ContextVar is only valid during `MeridianConfig()` construction.**
It carries load context into field validators. Never read it outside `settings.py` field
validators or the source callables. Misuse causes `AttributeError` or silently wrong
paths depending on timing.

**Dynamic section merge strategies vary by section** — don't assume they all deep-merge:

| Section | Strategy | Effect |
|---|---|---|
| `agents`, `work`, `context` | `nested_dict` | Keys in higher-precedence file overwrite matching keys; others inherited |
| `hooks` | `replace` | Entire section from higher-precedence file wins |
| `workspace` | `external` | Not merged by settings |

**`preserving_edit.py` for in-place config edits.** Direct TOML writes lose comments
and formatting. Use `preserving_edit.py` when editing config programmatically.

## Entry Points

- `settings.py` — `MeridianConfig`, `load_config()`: start here
- `schema.py` — `config_field()`, `ConfigOptionMeta`: field metadata and parse helpers
- `project_root.py` — `resolve_project_root()`, `resolve_user_config_path()`
- `context_config.py` — `ContextConfig`, `ContextSourceType`: context path schema
- `catalog.py` — `build_option_catalog()`: introspects `MeridianConfig` for CLI/docs

### Harness Profile Config Settings

Per-harness profile configs under `HarnessConfig` expose TOML-settable fields. The
Claude profile includes:

- `[harness.claude] model` — default model (string, env: `MERIDIAN_HARNESS_MODEL_CLAUDE`)
- `[harness.claude] wait_yield_seconds` — polling interval (float, env:
  `MERIDIAN_HARNESS_WAIT_YIELD_SECONDS_CLAUDE`)
- `[harness.claude] allow_builtin_agents` — whether to permit Claude's built-in
  subagents (Explore, Plan, general-purpose). Default `false` — Meridian denies them
  so sessions use custom Meridian agents exclusively. Set `true` to opt out.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — ContextVar thread-local pattern,
precedence chain implementation, dynamic section merge strategies, `MeridianConfig`
vs `RuntimeOverrides` separation

## Related

- `../core/overrides.py` — `RuntimeOverrides`: per-spawn behavioral choices
- `../context/resolver.py` — consumes `ContextConfig` from `context_config.py`
- `../catalog/` — uses `load_config()` output for model resolution defaults
