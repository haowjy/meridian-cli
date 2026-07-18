# lib/context/

Converts symbolic context configuration (`ContextConfig`) into concrete filesystem
paths. The rest of the system uses these resolved paths to locate work directories,
KB roots, and named contexts. This module derives paths — it does not create them,
clone repos, or trigger any filesystem side effects.

## Mental Model

`ContextConfig` describes *what* a context is (type, remote URL, path template).
`ResolvedContextPaths` is *where it lives on disk*. The resolver is a pure translation
layer between the two.

Agents receive context paths via env vars set at spawn time. The naming convention
is `MERIDIAN_CONTEXT_{NAME}_DIR`. `context_env_key(name)` derives the var name.

## Key Rules

**`{project}` is resolved from `meridian.toml` identity.** Read-only use does not create identity. When identity is absent, unresolved projection paths are never created; write paths create identity before resolving context state.

**`check_env=False` is for prompt injection, not human display.** `render_context_lines()`
with `check_env=False` always emits `$ENV_VAR (resolved_path)` — verbose and not
intended for terminal output. Use the default `check_env=True` for human-readable display.

**Git-backed contexts do not trigger cloning.** When `source == ContextSourceType.GIT`,
`_resolve_path()` only derives the expected path from `resolve_clone_path(remote)`.
Actual cloning is handled by git-autosync hooks at a different layer.

Paths resolve via `[context.work]` and `[context.kb]` configuration. Defaults are user-scoped:
- `work_root` → `{user_home}/context/{project}/work`
- `kb_root` → `{user_home}/context/{project}/kb`

When `{project}` cannot resolve, no repo-local context fallback is used. Extra contexts requiring identity are skipped, and no state is created.

## Entry Points

- `resolver.py` — `resolve_context_paths()`, `render_context_lines()`, `context_env_key()`

Key types:
- `ResolvedContextPaths` — resolved paths (output)
- `ContextConfig` (from `../config/context_config.py`) — config schema (input)

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — placeholder substitution rules,
git-backed resolution, identity-free read behavior, env var naming convention, rendering modes

## Related

- `../config/context_config.py` — `ContextConfig`, `ContextSourceType` (input schema)
- `../state/user_paths.py` — `get_project_id()` (project ID lookup)
- `../plugin_api/git.py` — `resolve_clone_path()` (git-backed clone location)
