# lib/config/

`MeridianConfig`: project-wide operational config (TOML + env), loaded at startup. Not
`RuntimeOverrides` (`../core/overrides.py`), which holds per-spawn choices assembled fresh
per spawn; never move per-spawn policy into `MeridianConfig`.

Precedence, low to high: defaults < user (`~/.meridian/config.toml`) < project
(`meridian.toml`) < local (`meridian.local.toml`) < env. `load_config()` is the entry point.

## Rules

- **The models are the only validation path.** Every config shape is a Pydantic model rooted
  at `MeridianConfig`; field types and validators carry the constraints. Unknown-key
  warnings come from one `extra="allow"` pass (`model_extra`); never walk annotations
  separately and never add a hand-written normalizer for a shape a model declares. Report
  failures through `errors.py` so messages name the key and file.
  `config_silences_extras` is the only warning-policy `ClassVar`.
- **No implicit Pydantic settings sources** (no `pydantic-settings`) or ambient loader state.
  Hook rows are carried unvalidated; `lib/hooks/config.py` is their one validation and
  runtime-loading boundary.
- **A field without `config_field()` metadata has no env var and no `config get/set`**:
  `_env_alias_overrides()` reads env vars only from `OPTION_CATALOG`.
- **Dynamic sections don't all deep-merge**: `work`, `context`, `env` are `nested_dict`
  (per-key overwrite); `hooks` is `replace` (highest-precedence raw section); `workspace` is
  `external` (not merged here).
- **Edit `meridian.toml` through `mutate_project_config()`** (`preserving_edit.py`): direct
  TOML writes lose comments, and it serializes the read-modify-write under a project-keyed
  lock in user home. New locks follow the stable-inode rule in
  [../state/AGENTS.md](../state/AGENTS.md).

[.context/CONTEXT.md](.context/CONTEXT.md) — mutation transaction, merge rules, harness
profile settings, module map.
