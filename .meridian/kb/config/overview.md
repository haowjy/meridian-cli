# config/ — Settings and Runtime Overrides

## Two-Layer Config Architecture

Meridian separates "loaded settings" (from TOML files) from "runtime overrides" (from CLI flags, env vars, agent profiles). These are distinct types with different precedence semantics.

### MeridianConfig (loaded settings)

`MeridianConfig` in `src/meridian/lib/config/settings.py` — `pydantic_settings.BaseSettings` subclass.

**Precedence** (highest to lowest):
1. Environment variables (`MERIDIAN_*`)
2. Project config: `.meridian/config.toml`
3. User config: `~/.meridian/config.toml` (or `MERIDIAN_CONFIG` env var)
4. Built-in defaults

Loaded via `load_config(project_root, user_config=None)`. Uses a `ContextVar` to thread `project_root` through validation so model identifiers in config files can be resolved at load time.

**Key fields**:

```toml
# .meridian/config.toml or ~/.meridian/config.toml

max_depth = 3                          # max spawn nesting depth
max_retries = 3
retry_backoff_seconds = 0.25
kill_grace_minutes = 0.033             # ~2 seconds
guardrail_timeout_minutes = 0.5        # 30 seconds
wait_timeout_minutes = 30.0

primary_agent = "__meridian-orchestrator"   # for `meridian` CLI (no spawn)
default_agent = "__meridian-subagent"       # for spawned subagents

default_model = ""                     # empty = harness picks
default_harness = "codex"

[harness]
claude = "claude-sonnet-4-5"           # default model per harness
codex = "gpt-4o"
opencode = "gemini-pro"

[primary]
model = "..."                          # overrides for primary (CLI) launch
harness = "..."
agent = "..."
effort = "high"
approval = "auto"
autocompact = 80
timeout = 3600.0

[output]
verbosity = "normal"                   # quiet | normal | verbose | debug
format = "text"
show = ["model", "harness"]
```

TOML config supports section aliases: `[defaults]` maps to top-level fields; `[timeouts]` maps to timeout fields. Unknown keys are warned and ignored (not errors).

### RuntimeOverrides (per-spawn overrides)

`RuntimeOverrides` in `src/meridian/lib/core/overrides.py` — plain `BaseModel`.

**Fields**: `model`, `harness`, `agent`, `effort`, `sandbox`, `approval`, `autocompact`, `timeout` (all optional).

**Sources** (loaded via factory classmethods):
- `RuntimeOverrides.from_env()` — `MERIDIAN_MODEL`, `MERIDIAN_HARNESS`, `MERIDIAN_EFFORT`, `MERIDIAN_SANDBOX`, `MERIDIAN_APPROVAL`, `MERIDIAN_AUTOCOMPACT`, `MERIDIAN_TIMEOUT`
- `RuntimeOverrides.from_launch_request(request)` — CLI flags (`-m`, `--approval`, etc.)
- `RuntimeOverrides.from_agent_profile(profile)` — agent profile frontmatter
- `RuntimeOverrides.from_config(config)` — `config.primary.*` fields (for primary launch)
- `RuntimeOverrides.from_spawn_config(config)` — `config.default_*` fields (for spawned agents)
- `RuntimeOverrides.from_spawn_input(payload)` — `spawn create` API payload

**Merge**: `resolve(*layers: RuntimeOverrides)` → first-non-`None` wins per field. Layer order matters: higher-priority layers go first.

## Precedence in Practice

The full precedence chain for a spawn (highest to lowest):

```
CLI flags / spawn input        → RuntimeOverrides.from_launch_request()
Environment variables          → RuntimeOverrides.from_env()
Agent profile frontmatter      → RuntimeOverrides.from_agent_profile()
config.primary.*               → RuntimeOverrides.from_config()  (primary launch only)
config.default_*               → RuntimeOverrides.from_spawn_config()  (spawn only)
```

This is applied by `resolve_policies()` in `launch/resolve.py` as a two-pass process:
1. Pre-profile merge to determine which agent profile to load
2. Re-merge with profile overrides included

This two-pass design is required because the profile may influence model/harness selection, but the profile itself is selected based on a pre-profile agent name resolution.

## resolve_project_root()

`resolve_project_root(explicit=None)` in `src/meridian/lib/config/project_root.py` determines the project root:

1. Explicit argument
2. `MERIDIAN_PROJECT_DIR` env var
3. Walk up from cwd; first directory matching any of:
   - `.mars/` directory (mars package marker)
   - `meridian.toml` file
   - `meridian.local.toml` file
   - `.meridian/id` file (project state marker)
   - `.git` entry (repo boundary)
4. Fallback to cwd

Provenance of the discovery is recorded in `ProjectRootResolution.source` (one of `explicit`, `env`, `mars`, `meridian-toml`, `meridian-local-toml`, `project-state`, `git`, `cwd`).

## Config CLI

`meridian config set/get/reset` works on project config (`.meridian/config.toml`), not user config. `config.show` annotates each resolved value with its source (builtin / user / project / env).

`config.init` (alias: `meridian init`) seeds `.meridian/` directories, `.gitignore`, and runs `mars init/link` for first-run bootstrap. The `--link` flag symlinks `.agents/` into an external tool directory (e.g., for IDE integration).

## Workspace Config

`src/meridian/lib/config/workspace.py` — multi-repo context injection via named workspace entries. Parsed on every launch to extend harness file access beyond the project root.

### File Locations

Workspace config uses two TOML files at the project root (not inside `.meridian/`):

- **`meridian.toml`** — committed entries; describes expected repository layout shared with the team
- **`meridian.local.toml`** — local additions/overrides; machine-specific, never committed (gitignored by default)

`resolve_project_config_paths(project_root)` resolves both: `.meridian_toml` → `<project_root>/meridian.toml`, `.meridian_local_toml` → `<project_root>/meridian.local.toml`.

### Schema

Each workspace entry is a named TOML table `[workspace.<name>]` with a required `path` field. Entry names must match `^[a-z][a-z0-9_-]*$`. Unknown keys are collected as findings and ignored.

```toml
# meridian.toml — committed
[workspace.frontend]
path = "../meridian-web"

[workspace.prompts]
path = "../prompts/meridian-base"

# meridian.local.toml — local additions/overrides
[workspace.frontend]
path = "../my-fork/meridian-web"   # overrides committed path for this machine

[workspace.local-data]
path = "/data/large-dataset"       # local-only entry
```

There is no `enabled` field and no subtractive override — a committed entry can only be redirected by a local entry with the same name, not disabled.

### Two-Layer Merge

`resolve_workspace_snapshot(project_root) → WorkspaceSnapshot`:

1. Reads committed entries from `meridian.toml` and local entries from `meridian.local.toml`
2. Merges: local entries with the same name override the committed path; local-only entries are appended after committed entries
3. Resolves each declared path (relative paths resolved against `project_root`; `~` expanded), checks `is_dir()`

```
WorkspaceSnapshot
  status: "none" | "present" | "invalid"
  source_paths: tuple[Path, ...]
  roots: tuple[ResolvedWorkspaceRoot]
    - name, declared_path, resolved_path
    - enabled (always True), exists, source ("committed" | "local" | "merged")
```

`status = "invalid"` (TOML parse or schema error) blocks launch. `"none"` (no workspace tables) and `"present"` (parsed OK) do not.

### Findings (non-blocking diagnostics)

- `workspace_unknown_key` — unrecognized keys inside a workspace entry (typo guard)
- `workspace_missing_root` — a committed entry path does not exist on disk
- `workspace_local_missing_root` — a local or merged entry path does not exist (likely stale override)

Surface in `meridian config show` and `meridian doctor`; do not block launch.

### Projectable Roots

`get_projectable_roots(snapshot) → tuple[Path, ...]` — filters to enabled + existing roots. Input to the harness projection stage in `launch/`. See `launch/overview.md`.

### Config Show Integration

`lib/ops/config_surface.py:build_config_surface()` calls `resolve_workspace_snapshot()` and wraps the result into `ConfigSurface.workspace` (`ConfigSurfaceWorkspace`): status, source paths, root tallies, and per-harness `WorkspaceApplicability`.

### Workspace Init

`meridian workspace init` — `lib/ops/workspace.py:workspace_init_sync()`. Creates or appends to `meridian.local.toml` with example `[workspace]` entries, then ensures `.git/info/exclude` contains the gitignore entries. Single write path; everything else is read-only.

## Why Two Separate Systems

`MeridianConfig` handles persistent, file-backed configuration that survives across sessions. `RuntimeOverrides` handles ephemeral per-spawn flags that override file config. They're separate because:

1. `MeridianConfig` fields include operational settings (timeouts, depths) that don't belong in override layers
2. `RuntimeOverrides` needs to be passed through the spawn hierarchy and merged cleanly
3. File config is loaded once at startup; override resolution runs per-spawn

This keeps the "which model to use for this spawn" question distinct from "what is the system's retry policy."
