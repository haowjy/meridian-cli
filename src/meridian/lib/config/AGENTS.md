# lib/config/

Owns project-wide operational configuration: `MeridianConfig`, the TOML+env
precedence chain, and config schema introspection. Loads at startup and applies
broadly. This is not the same system as per-spawn runtime overrides.

## Mental Model: Two Config Systems — Don't Conflate

**`MeridianConfig`** — persistent, project-wide settings: timeouts,
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

Merged low to high: each later layer overrides the earlier ones, so env vars beat
local beats project beats user. `load_config()` is the public entry point.

## Key Rules

**Use stdlib `logging.getLogger(__name__)`, not structlog.** The launch diagnostic
boundary (`capture_library_diagnostics()`) captures stdlib warnings during spawn.
Structlog bypasses it and leaks to stderr. This rule applies to both `lib/config/`
and `lib/catalog/`.

**File validation and source merging are explicit.** `loader.py` validates each file
against `MeridianConfig`, then merges user → project → local → env.
`load_config_with_sources()` retains typed dynamic sections and field provenance for
inspection; `load_config()` returns its final `MeridianConfig`. Hook rows displayed
by `config show` come from the hook owner's load report so their names and invalid-row
count match hook commands. Do not add implicit
Pydantic settings sources (no `pydantic-settings`) or ambient loader state. Hook
rows are carried unvalidated for inspection; `lib/hooks/config.py` is their one
validation and runtime-loading boundary.

**The models are the only validation path.** Every operational config shape is a Pydantic model
(`models.py`, `harness_models.py`, `history_models.py`) rooted at `MeridianConfig`,
including `[env]` and the non-hook dynamic sections' descriptor models. Field types and
validators carry the constraints. Unknown-key warnings come from one
`extra="allow"` validation pass and its `model_extra` values, with Pydantic owning
the traversal; never walk annotations separately. `config_silences_extras` is the
only warning-policy `ClassVar`. The real validation pass still makes
`extra="forbid"` models hard-fail. File aliases and env-var names are `config_field()`
metadata. Never add a hand-written normalizer that re-checks a shape a model already
declares: two validators for one shape drift apart. Format validation failures
through `errors.py` so every message names the key and file.

**A field without `config_field()` metadata has no env var and no `config get/set`.**
`_env_alias_overrides()` reads env vars only from `OPTION_CATALOG`, built from that
metadata, so a field or harness profile built outside the catalog silently loses
its env vars.

**Dynamic section merge strategies vary by section** — don't assume they all deep-merge:

| Section | Strategy | Effect |
|---|---|---|
| `work`, `context`, `env` | `nested_dict` | Keys in higher-precedence file overwrite matching keys; others inherited |
| `hooks` | `replace` | Retains the highest-precedence raw section for provenance; hook rows themselves are inspected and run through `lib/hooks/config.py` |
| `workspace` | `external` | Not merged here; workspace config owns it |

**`preserving_edit.py` for in-place config edits.** Direct TOML writes lose comments
and formatting. Use `mutate_project_config()` when editing `meridian.toml`; it
serializes the full read-modify-write transaction with a project-root-keyed lock
under the user Meridian home. Lower-level preserving editors belong in the same
module and run inside that transaction.

## Entry Points

- `models.py` — core section models and the `MeridianConfig` schema root
- `harness_models.py` — generated per-harness profiles and idle settings
- `history_models.py` — history retention and archive settings
- `model_base.py` — shared model warning policy and value normalization
- `loader.py` — TOML/env validation, precedence merge, provenance, and load entry points
- `layers.py` — shared user/project/local discovery and TOML reads
- `dynamic_sections.py` — descriptor models for hooks/work/context/workspace/env plus their merge policy
- `errors.py` — the one validation-error format (key path and file)
- `schema.py` — `config_field()`, `ConfigOptionMeta`: field metadata and parse helpers
- `project_root.py` — `resolve_project_root_resolution()`, `resolve_user_config_path()`
- `context_config.py` — `ContextConfig`, `ContextSourceType`: context path schema
- `catalog.py` — `build_option_catalog()`: introspects `MeridianConfig` for CLI/docs

### Harness Profile Config Settings

Per-harness profile configs under `HarnessConfig` expose TOML-settable fields. The
Claude profile includes:

- `[harness.claude] model` — default model (string, env: `MERIDIAN_HARNESS_MODEL_CLAUDE`)
- `[harness.claude] wait_yield_seconds` — polling interval (float, env:
  `MERIDIAN_HARNESS_WAIT_YIELD_SECONDS_CLAUDE`)
- `[harness.<id>.idle] enabled` — per-harness idle toggle. Each profile uses a model
  built by `harness_idle_model()` so its canonical key, file alias, and env name stay unique.
- `[harness.<claude|codex>] allow_builtin_agents` (default `false`) — whether the
  harness's own subagents are available. Meridian is the orchestrator, so
  harness-native subagents are off by default: Claude denies built-in `Agent`
  types; Codex launches with `-c features.multi_agent_v2=false -c agents.max_depth=0`
  (on Codex 0.162 `features.multi_agent=false` leaves the v1 tools available, and
  `--disable` would fail on a Codex build that lacks the feature). Every harness
  profile parses the key, but only Claude and Codex act on it.
- Generic Claude `Agent` routing is not a `meridian.toml` harness policy: it follows
  Mars `[settings.meridian.agent_copy]` with `.claude` target materialization. Built-in
  Claude agent types are denied unless `allow_builtin_agents = true`.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — project-config mutation transaction,
precedence chain implementation, dynamic section merge strategies, `MeridianConfig`
vs `RuntimeOverrides` separation

## Related

- `../core/overrides.py` — `RuntimeOverrides`: per-spawn behavioral choices
- `../context/resolver.py` — consumes `ContextConfig` from `context_config.py`
- `../catalog/` — uses `load_config()` output for model resolution defaults
