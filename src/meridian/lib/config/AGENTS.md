# lib/config/

Operational configuration loading, schema, and path resolution. Owns `MeridianConfig`
(project-wide settings) and the TOML/env precedence chain. Does not own per-spawn
runtime overrides — those live in `../core/overrides.py`.

## Entry Points

- `settings.py` — `MeridianConfig`, `load_config()`: main config model and loader
- `schema.py` — `config_field()`, `ConfigOptionMeta`, parse/coerce helpers
- `catalog.py` — `build_option_catalog()`: introspects `MeridianConfig` fields for CLI/docs
- `context_config.py` — `ContextConfig`, `ContextSourceType`: context path config schema
- `project_root.py` — `resolve_project_root()`, `resolve_user_config_path()`
- `project_paths.py` — `ProjectConfigPaths`, `resolve_project_config_paths()`
- `project_config_state.py` — legacy/new config file detection
- `preserving_edit.py` — TOML-aware in-place config edits
- `workspace.py` — workspace section handling
- `settings.py:DYNAMIC_SECTION_DESCRIPTORS` — merge rules for agents/hooks/work/context/workspace

## Depth

See [.context/CONTEXT.md](.context/CONTEXT.md) for:
- Config precedence chain (user < project < local < env)
- ContextVar thread-local loading pattern
- Dynamic section merge strategies (nested_dict vs replace vs external)
- `MeridianConfig` vs `RuntimeOverrides` distinction
- Logging convention: stdlib `logging`, not structlog

## Related

- `../core/overrides.py` — `RuntimeOverrides`: per-spawn behavioral choices (different system)
- `../context/resolver.py` — consumes `ContextConfig` from `context_config.py`
- `../catalog/` — uses `load_config()` output for model resolution defaults
