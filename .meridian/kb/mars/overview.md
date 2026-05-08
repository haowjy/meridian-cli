# mars/ — Mars Integration

Mars (`mars-agents`) is an external Rust binary that manages the `.agents/` directory — syncing agent profiles and skill content from declared package sources into a project-local managed root. Meridian depends on it for agent/skill distribution and model alias resolution.

Mars is a hard dependency for spawn-time model resolution — `resolve_model()` raises `RuntimeError` if the mars binary is absent. Model *listing* (`load_mars_aliases()`) degrades gracefully: it tries `mars models list --json` first, falls back to reading `.mars/models-merged.json` if the binary is unavailable, and returns an empty list if neither works. Bootstrap (`mars init`, `mars link`) also degrades — it suppresses `FileNotFoundError` and continues without mars.

## What Mars Manages

```
.agents/
  agents/       agent profile YAML-markdown files (e.g., coder.md, reviewer.md)
  skills/       skill YAML-markdown files (e.g., __meridian-spawn/SKILL.md)
mars.toml       package source declarations
.mars/
  models-merged.json   merged model alias definitions (fallback when mars binary absent)
```

Mars reads `mars.toml` and materializes content from declared sources (git repos, local paths) into `.agents/`. Meridian reads `.agents/agents/` and `.agents/skills/` at spawn time to load profiles and inject skills.

## Binary Resolution

`cli/main.py:_resolve_mars_executable()`:
1. Check `Path(sys.executable).parent` — the scripts directory of the active Python install — for `mars` or `mars.exe`
2. Fall back to `shutil.which("mars")`

**Why prefer the scripts dir?** When meridian is installed as a `uv tool`, `sys.executable` is a symlinked Python binary. Following the symlink would jump out of the tool's isolated environment where the bundled `mars` sibling lives. Staying in `Path(sys.executable).parent` keeps both binaries in the same environment.

`_run_mars_passthrough(args)` runs the resolved binary with `subprocess.run`, exits with its return code. If the binary is not found, it prints an error to stderr and exits 1.

## Bootstrap Integration

`ops/config.py:_ensure_mars_init(project_root, link)` runs during `ensure_state_bootstrap_sync()`:

```python
if not mars_toml.exists():
    subprocess.run([mars_bin, "init", "--json", *link_flags], cwd=project_root)
elif link:
    for d in link:
        subprocess.run([mars_bin, "link", d], cwd=project_root)
```

- If `mars.toml` is absent: runs `mars init` to scaffold an empty managed root. `--link <dir>` flags are forwarded if `--link` was passed to `meridian init`.
- If `mars.toml` exists and link dirs are requested: runs `mars link <dir>` for each — idempotent, handles the "already initialized, add a new link" case.
- Both calls use `check=False` and `contextlib.suppress(FileNotFoundError)` — if mars is absent, the bootstrap continues without it.

`config init --link <dir>` is how a user wires `.agents/` into an external tool directory (e.g., Claude Code's tool root) during project setup.

## Model Alias Resolution

`lib/catalog/model_aliases.py` queries mars for known aliases at spawn resolution time:

1. Run `mars models list --json` — returns structured alias definitions
2. If that fails (binary absent, non-zero exit): fall back to `.mars/models-merged.json`
3. If that also fails: return empty alias table

This means `meridian spawn -m sonnet` can resolve `sonnet` to `claude-sonnet-4-5` even if the mars binary isn't on PATH, as long as `.mars/models-merged.json` was written by a prior `meridian mars sync`.

**Why does mars own model aliases?** Because alias definitions travel with the agent package — the same `mars.toml` that installs the `coder` agent also declares which model names that agent profile was designed for. Centralizing alias management in mars lets teams override `sonnet` → their preferred model once, in one config file, and have all spawns pick it up.

The resolved alias also feeds `spawn.create` validation: if a model name is unknown, prepare.py uses the alias table to suggest nearby matches.

## Sync Workflow

`meridian mars sync` (passthrough to `mars sync`):
- Reads `mars.toml` package sources
- Fetches/updates each source
- Materializes content into `.agents/`
- Handles local edit conflicts and merge flows

This is a manual step — meridian does not auto-sync on spawn. The intended workflow is: edit source submodules → commit → push → `meridian mars sync` → `.agents/` is regenerated.

**Why not auto-sync?** Auto-sync on spawn would make every spawn potentially slow (git fetch) and non-deterministic (content changes mid-session). Explicit sync is predictable and fast.

## Doctor Checks

`ops/diag.py:doctor()` warns when:
- `.agents/agents/` or `.agents/skills/` directories are missing
- A configured `primary_agent` or `default_agent` profile is absent from `.agents/agents/`
- Legacy install artifacts are present (`.meridian/agents.toml`, `.meridian/cache/agents/`)

It does not attempt to run `mars sync` — diagnosis only.

## Relationship to Catalog

`lib/catalog/agent.py` and `lib/catalog/skill.py` load profiles from `.agents/` at spawn time. They warn (not error) on missing files so spawns can proceed with degraded configuration rather than failing. The default agent policy (`lib/launch/default_agent_policy.py`) warns when no suitable default agent is found in `.agents/agents/`.

See `../catalog/overview.md` for the full profile loading and model resolution pipeline.
