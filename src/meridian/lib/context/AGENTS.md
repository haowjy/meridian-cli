# lib/context/

Context path resolution — converts symbolic context config (work, kb, extra named
contexts) into concrete filesystem paths for use by agents and CLI commands.

## Entry Points

- `resolver.py` — `resolve_context_paths()`, `render_context_lines()`, `context_env_key()`

## Key Types

- `ResolvedContextPaths` — resolved work root, work archive, kb root, and extra contexts
- `ContextConfig` (from `../config/context_config.py`) — the config input to resolver

## Depth

See [.context/CONTEXT.md](.context/CONTEXT.md) for:
- Placeholder substitution rules (`{project}`, `{user_home}`)
- Git-backed context resolution
- Fallback behavior when project ID is missing
- Env var naming convention (`MERIDIAN_CONTEXT_{NAME}_DIR`)
- `check_env` vs prompt-injection rendering modes

## Related

- `../config/context_config.py` — `ContextConfig`, `ContextSourceType` (input schema)
- `../state/user_paths.py` — `get_project_id()` (project ID lookup)
- `../plugin_api/git.py` — `resolve_clone_path()` (git-backed context clone location)
