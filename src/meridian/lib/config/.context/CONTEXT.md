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

**`MeridianConfig`** — persistent, project-wide operational settings: timeouts,
output verbosity, state retention, default model/harness. Loaded from TOML
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

`load_config()` validates each TOML file against `MeridianConfig` (so an error names
the file), then deep-merges the validated payloads from lowest to highest precedence:

```python
user → project → local → environment
```

Higher-precedence dictionaries replace or recursively overlay lower-precedence
dictionaries. Lists replace. The final merged payload is validated into
`MeridianConfig`; there is no ambient load context or re-entrant settings source.
`layers.iter_config_layers()` is the single user/project/local discovery and TOML-read
seam shared by the loader, hook registry, and config inspection.

`load_config_with_sources()` also retains `DynamicConfigSections` plus one
`ConfigValueSource` per explicitly set field. Inspection commands render those validated
values and sources. The hook row exception delegates names and invalid-row counts to
`load_hooks_config_report()`, their validation owner, while the dynamic value retains
the provenance of the explicitly configured section. An empty higher-precedence
`hooks` section does not suppress lower-layer rows in either inspection or runtime.

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
| `hooks` | `replace` | Later raw section supplies provenance; `config show` and hook runtime use the hook owner's layered rows |
| `work` | `nested_dict` | Deep merge: worktree_base etc. overwrite per-key |
| `context` | `nested_dict` | Deep merge: named contexts overwrite per-context-name |
| `workspace` | `external` | Not merged by the loader — workspace config owns it |
| `env` | `nested_dict` | Deep merge: higher-precedence values overwrite per variable |

For `nested_dict`, keys in the local config overlay keys in the project config;
both overlay the user config.

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

Claude native Agent routing has separate controls. The `[harness.claude]` table's
`allow_builtin_agents` option controls the scoped built-in `Agent(...)` denies
(default `false`). Generic `Agent` permission follows Mars
`[settings.meridian.agent_copy]` via `agent_copy_enabled()` in
`lib/catalog/agent_copy.py`.

## Schema Metadata (`schema.py`)

`config_field(canonical_key, value_kind=..., file_aliases=..., env_vars=...)` attaches
metadata to `MeridianConfig` fields. The metadata is introspected by `build_option_catalog()`
into `OPTION_CATALOG`, which drives `meridian config` get/set/reset, the docs, and the
env-var overlay: `loader._env_alias_overrides()` reads only catalog env names. A field
with no metadata validates from TOML but has no env var and no generic CLI access.

`ValueKind` types: `"int"`, `"float"`, `"str"`, `"str_list"`, `"verbosity"`.

`parse_toml_scalar()`, `parse_cli_scalar()`, `parse_env_scalar()` do type-safe
coercion with source-specific error messages. Always pass the source name for
debuggable errors.

## Related KB

- `$MERIDIAN_CONTEXT_KB_DIR/concepts/config-precedence.md` — full precedence chain and per-spawn vs project config distinction (see `meridian context kb`)
