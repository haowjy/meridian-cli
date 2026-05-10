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

**`{project}` placeholder resolves to `None` if no project ID is available.** A fresh
project without `.meridian/project_id` produces `None` for any path containing
`{project}`. `resolve_context_paths()` handles this gracefully with fallbacks, but
if you call `_resolve_path()` directly, you must handle `None` returns.

**`check_env=False` is for prompt injection, not human display.** `render_context_lines()`
with `check_env=False` always emits `$ENV_VAR (resolved_path)` — verbose and not
intended for terminal output. Use the default `check_env=True` for human-readable display.

**Git-backed contexts do not trigger cloning.** When `source == ContextSourceType.GIT`,
`_resolve_path()` only derives the expected path from `resolve_clone_path(remote)`.
Actual cloning is handled by git-autosync hooks at a different layer.

**Fresh projects always produce valid `ResolvedContextPaths`.** When `{project}` cannot
resolve, fallbacks kick in:
- `work_root` → `<project_root>/.meridian/work`
- `kb_root` → `<project_root>/.meridian/kb`
- Extra contexts requiring `{project}` → silently skipped

Callers can assume a non-null result but must not assume the paths exist on disk.

## Entry Points

- `resolver.py` — `resolve_context_paths()`, `render_context_lines()`, `context_env_key()`

Key types:
- `ResolvedContextPaths` — resolved paths (output)
- `ContextConfig` (from `../config/context_config.py`) — config schema (input)

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — placeholder substitution rules,
git-backed resolution, fallback behavior, env var naming convention, rendering modes

## Related

- `../config/context_config.py` — `ContextConfig`, `ContextSourceType` (input schema)
- `../state/user_paths.py` — `get_project_id()` (project ID lookup)
- `../plugin_api/git.py` — `resolve_clone_path()` (git-backed clone location)
