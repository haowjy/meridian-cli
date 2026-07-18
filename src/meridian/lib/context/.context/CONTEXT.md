# lib/context/ — Context

Context path resolution converts `ContextConfig` into concrete `Path` objects.
The resolved paths are what agents and CLI commands use to locate work, kb, and
other named context directories.

## Contracts

### Placeholder Substitution

`_resolve_path()` supports two placeholders:

- `{project}` — replaced by the project's stable `[project] id` from `meridian.toml`. Returns `None` when identity is absent; read paths remain stateless and write paths create identity first.
- `{user_home}` — replaced by `get_user_home()`. Always available.

Paths without placeholders resolve relative to `project_root` if not absolute.

### Git-Backed Contexts

When `source == ContextSourceType.GIT` and a `remote` URL is configured, the path
resolves relative to the auto-cloned repository at `resolve_clone_path(remote)`.
The clone itself is handled lazily by git-autosync hooks — `_resolve_path()` only
derives the expected path; it does not trigger cloning.

### Identity-Free Reads

No repo-local context fallback exists. Without identity, read-only projection uses non-materialized unresolved paths and extra `{project}` contexts are skipped.

### Env Var Naming Convention

`context_env_key(name)` derives `MERIDIAN_CONTEXT_{NAME}_DIR` from a context name.
Non-alphanumeric characters in the name become underscores. This convention is how
agents receive context paths at spawn time — env vars are set before the agent reads
any prompt.

Special cases:
- `work` → `MERIDIAN_ACTIVE_WORK_DIR` (active scope dir: named work item when attached, else ambient spawn scope `spawns/p<N>/work`; not derived by `context_env_key`)
- `work_archive` → `MERIDIAN_CONTEXT_WORK_ARCHIVE_DIR`
- `kb` → `MERIDIAN_CONTEXT_KB_DIR`

### Rendering Modes

`render_context_lines()` has two modes controlled by `check_env`:

- `check_env=True` (default — CLI display): shows `$ENV_VAR` when the env var is set
  and matches the resolved path; otherwise shows the raw resolved path.
- `check_env=False` (prompt injection): always shows `$ENV_VAR (resolved_path)`
  regardless of current env state. Use this when building the agent's system prompt —
  the env vars will be set by launch time.

Do not use `check_env=False` for display to humans — the format is verbose and
includes paths even when env vars are already set.

## Related KB

- `$MERIDIAN_CONTEXT_KB_DIR/concepts/context-resolution.md` — cross-cutting context design (see `meridian context kb`)
