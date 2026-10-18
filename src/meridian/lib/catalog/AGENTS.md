# lib/catalog/

Translates model aliases and agent profile names into concrete execution parameters
before any process launches. This layer is the bridge between what users or config
files specify ("gpt55", "coder") and what the launch layer needs to build a command.
Mars resolves skills as part of the launch bundle; Meridian has no filesystem skill
catalog.

Zero builtin aliases. All alias-to-model-id mappings come from mars packages
(`.mars/models-merged.json`). Do not add hardcoded aliases to this module.

`mars_roots.py` is the only package-root decider. Project content follows Mars's
nearest-`mars.toml` walk-up; otherwise the primary root is the user package project
at `~/.meridian/mars`. Project-scope discovery and launch subprocesses include user
content through `--fallback-root` only when `mars_capabilities.py` confirms that the
installed Mars supports it. The primary root remains the sole configuration
authority. Every Mars subprocess that touches package content receives the resolved
primary as `--root`. Two exceptions: `meridian mars init` / `init --link` target the
literal project root, so a nested Mars project can still be created in a repository
subdirectory, and the `--help` capability probe takes no root.
`ensure_user_mars_root()`
belongs only on Mars operations that need a runnable root; read-pure diagnostics
such as bare-directory `doctor` must report the resolved path without initializing it.

## Mental Model

Every launch operation creates a `CatalogSession`, uses it for all lookups in that
operation, then discards it. The session holds a `MarsResultCache` scoped to its
lifetime — this prevents redundant mars subprocess calls within one CLI invocation
and prevents stale results from leaking across invocations.

The legacy direct-resolution pipeline (launch routing uses the bundle):
1. Call `mars models resolve <name>` via subprocess.
2. If Mars resolves to a different ID, check `mars models catalog --json` for
   the exact literal ID. Return that literal ID with an unresolved harness if
   found; otherwise keep Mars's resolution.
3. If Mars reports an unknown alias, pass the input through as a raw model ID
   with an unresolved harness.

## Key Rules

**`MarsResultCache` is per-operation, not module-level.** Create it in
`CatalogSession.__init__`. A module-level singleton would cache across CLI invocations
and prevent alias updates from taking effect until restart.

**`run_mars_models_resolve` raises on mars unavailability; aliases/catalog queries
return `None`.** Mars is always bundled — unavailability is a hard error for
resolution but degrades gracefully for inventory. Do not add a soft fallback to resolve.

**Use `AliasEntry.harness` only when a harness is required; it raises if absent.**
Static aliases may have no authored harness preference, and catalog exact-ID
matches have no route. Do not guess from model-name patterns; runtime routing
comes from the launch bundle.

**`CatalogSession` is single-operation use.** Create one per launch operation and
discard at operation end. Do not share across operations — cache is intentionally
scoped.

## Fallback: `.mars/models-merged.json`

When the mars binary is unavailable, `load_mars_aliases()` reads
`.mars/models-merged.json` directly. Only pinned aliases (explicit `model` key) work
from the file; auto-resolve aliases require mars to expand. Fresh installs or offline
environments silently skip auto-resolve aliases.

## Entry Points

- `catalog_session.py` — `CatalogSession`: per-operation facade; start here
- `mars_roots.py` — project/user package-root resolution and user-root initialization
- `mars_capabilities.py` — cached help-surface probe for optional fallback lookup
- `model_aliases.py` — `AliasEntry`, `MarsResultCache`, mars subprocess integration
- `agent.py` — `AgentProfile`, agent frontmatter parsing for `.mars/agents/*.md`

## Usage Pattern

```python
session = CatalogSession(project_root)
alias_entry = session.resolve_model("gpt55")   # raises RuntimeError if mars unavailable
agent = load_agent_profile(project_root, "coder")
# discard session at operation end
```

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — mars subprocess integration, resolve vs
inventory asymmetry, `MarsResultCache` scoping contract, and fallback to merged JSON

## Related

- `../launch/compiler.py` — consumes `AliasEntry` and `AgentProfile` from this layer
- `../config/models.py` — config defaults fed into launch compiler
- `mars-agents` repo (sibling) — source of `.mars/agents/` and `.mars/models-merged.json`
