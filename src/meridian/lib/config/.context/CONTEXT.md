# lib/config/ — Context

Owns project-wide operational configuration (`MeridianConfig`) and the multi-source
TOML + env precedence chain. This is distinct from per-spawn runtime overrides
(`RuntimeOverrides` in `lib/core/overrides.py`).

## Architecture

### Project Config Mutation Transaction

Identity creation, identity migration, and `config set/reset` use
`preserving_edit.mutate_project_config()` (migration composes via its associated
transaction context). The transaction holds an exclusive
`platform.locking.lock_file()` across the read, preserving edit, and atomic
replace. Its stable lock lives at
`<user-home>/locks/project-config/<sha256(resolved-project-root)>.lock`: hashing
the canonical root avoids basename collisions without writing repo-local state
or depending on an identity that may not exist yet. The platform lock's
thread-local reentrancy lets identity migration and identity creation compose
without a second lock protocol or a cross-module `lock_held` flag.

### Two Config Systems — Don't Conflate

**`MeridianConfig`** — persistent, project-wide operational settings: retry policy,
timeouts, output verbosity, state retention, default model/harness. Loaded from TOML
files at startup. Applies broadly across all spawns.

**`RuntimeOverrides`** — per-spawn behavioral choices: model, harness, effort, approval,
sandbox, autocompact, timeout. Assembled fresh per spawn via the 7-tier precedence ladder.

The separation exists so "what are the project-wide defaults?" and "what should this
specific spawn do?" can be answered independently. Never move per-spawn routing or
policy resolution into `MeridianConfig`.

### Config Precedence Chain (lowest to highest)

```
hardcoded defaults (MeridianConfig field defaults)
  < user config (~/.meridian/config.toml)
    < project config (meridian.toml)
      < local config (meridian.local.toml)
        < environment variables
```

`load_config()` implements this by ordering `settings_customise_sources()` in
`MeridianConfig`:

```python
return (
    init_settings,          # highest — direct constructor kwargs
    layered_env_source,     # env vars
    local_toml_source,      # meridian.local.toml
    project_toml_source,    # meridian.toml
    user_toml_source,       # ~/.meridian/config.toml
)
```

pydantic-settings applies sources left-to-right, first-wins. Env vars beat local
beats project beats user.

### ContextVar Thread-Local Loading Pattern

`_SETTINGS_CONTEXT: ContextVar` carries the `_SettingsLoadContext` (project root,
config paths, user config path) into `MeridianConfig`'s field validators during
instantiation. This is necessary because pydantic-settings constructs models without
passing context to field validators.

**Pattern:**
```python
token = _SETTINGS_CONTEXT.set(_SettingsLoadContext(...))
try:
    return MeridianConfig()
finally:
    _SETTINGS_CONTEXT.reset(token)
```

Never read `_SETTINGS_CONTEXT` outside of `settings.py` field validators or the source
callables. It is only valid during `MeridianConfig()` construction.

### `resolve_project_root_resolution()` — `ignore_env` Parameter

`project_root.py:resolve_project_root_resolution()` accepts an `ignore_env`
flag (default `False`). When `True`, it skips the `MERIDIAN_PROJECT_DIR` env
var check and resolves project root to literal CWD (`source="cwd"`). There is
no marker walk-up.

**Why it exists:** Hooks commands (`cli/hooks_commands.py`) run inside spawned
processes that inherit the outer session's `MERIDIAN_PROJECT_DIR`. Without
`ignore_env=True`, those commands would resolve the spawning session's project
root — not the directory the hook is actually running in. Hooks need CWD-relative
resolution, regardless of what the parent process injected.

Use `ignore_env=True` only when the caller explicitly needs to resolve relative
to the actual CWD and must not honor an inherited session project directory.
All other callers should leave it at the default.

## Dynamic Section Merge Rules

Sections in `DYNAMIC_SECTION_DESCRIPTORS` use one of three merge strategies:

| Section | Strategy | Behavior |
|---|---|---|
| `agents` | `nested_dict` | Deep merge: overlay values overwrite matching agent names |
| `hooks` | `replace` | Later source entirely replaces earlier source |
| `work` | `nested_dict` | Deep merge: worktree_base etc. overwrite per-key |
| `context` | `nested_dict` | Deep merge: named contexts overwrite per-context-name |
| `workspace` | `external` | Not merged by settings — handled externally |

For `nested_dict`, keys in the local config overlay keys in the project config;
both overlay the user config. This lets a local `meridian.local.toml` add one
agent override without replacing all agent overrides from `meridian.toml`.

For `replace`, the entire section from the higher-precedence file wins.

## Logging Convention

`lib/catalog/` and `lib/config/` use stdlib `logging.getLogger(__name__)`. Do not
use `structlog` here. The launch diagnostic boundary (`capture_library_diagnostics()`)
captures stdlib warnings during spawn; structlog bypasses it and leaks to stderr.

## Launch-Time Harness Profile Resolution

Some harness profile fields are resolved at launch time rather than at config load
time. These follow the same pattern: ambient config snapshot → launch-time env override:

- `resolve_pi_harness_profile_for_launch()` — resolves `[harness.pi]` (managed bash,
  load_all_extensions, background_tasks, spawn_watch)

Called from `bind_launch_context()` in `lib/launch/context.py` and threaded
into the harness adapter's `resolve_launch_spec()`.

Claude native Agent routing does not use a `meridian.toml` harness option.
Built-in Claude agents stay denied unconditionally; generic `Agent` permission
follows Mars `[settings.meridian.agent_copy]` via `project_has_claude_agent_copy()` in
`lib/launch/permissions.py`.

## Schema Metadata (`schema.py`)

`config_field(canonical_key, value_kind=..., file_aliases=..., env_vars=...)` attaches
metadata to `MeridianConfig` fields. The metadata is introspected by `build_option_catalog()`
to generate the CLI `meridian config` commands and documentation.

`ValueKind` types: `"int"`, `"float"`, `"str"`, `"str_list"`, `"verbosity"`.

`parse_toml_scalar()`, `parse_cli_scalar()`, `parse_env_scalar()` do type-safe
coercion with source-specific error messages. Always pass the source name for
debuggable errors.

## Related KB

- `$MERIDIAN_CONTEXT_KB_DIR/concepts/config-precedence.md` — full precedence chain and per-spawn vs project config distinction (see `meridian context kb`)
