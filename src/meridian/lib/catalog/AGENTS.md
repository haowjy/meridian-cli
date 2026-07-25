# lib/catalog/

Translates symbolic names — model aliases, agent profile names, skill names — into
concrete execution parameters before any process launches. This layer is the bridge
between what users or config files specify ("gpt55", "coder") and what the launch
layer needs to build a command.

Zero builtin aliases. All alias-to-model-id mappings come from mars packages
(`.mars/models-merged.json`). Do not add hardcoded aliases to this module.

## Mental Model

Every launch operation creates a `CatalogSession`, uses it for all lookups in that
operation, then discards it. The session holds a `MarsResultCache` scoped to its
lifetime — this prevents redundant mars subprocess calls within one CLI invocation
and prevents stale results from leaking across invocations.

The resolution pipeline for a model alias:
1. `mars models resolve <name>` via subprocess
2. If unknown alias → search `mars models list --all` for exact model ID match
3. If not found → pass through as raw model ID with unresolved harness

## Key Rules

**`MarsResultCache` is per-operation, not module-level.** Create it in
`CatalogSession.__init__`. A module-level singleton would cache across CLI invocations
and prevent alias updates from taking effect until restart.

**`run_mars_models_resolve` raises on mars unavailability; the list variant returns
`None`.** Mars is always bundled — unavailability is a hard error for resolution but
degrades gracefully for listing. Do not add a soft fallback to resolve.

**Use `AliasEntry.harness` (property), not `.resolved_harness` (raw field).** The
property raises when `resolved_harness` is missing. Mars should always provide a
harness for resolved entries; missing harness is a bug, not a value meridian should
guess.

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
- `model_aliases.py` — `AliasEntry`, `MarsResultCache`, mars subprocess integration
- `agent.py` — `AgentProfile`, agent frontmatter parsing for `.mars/agents/*.md`
- `skill.py` — skill YAML/markdown parsing for `.mars/skills/*/SKILL.md` (legacy compat; primary path uses mars launch-bundle)

## Usage Pattern

```python
session = CatalogSession(project_root)
alias_entry = session.resolve_model("gpt55")   # raises RuntimeError if mars unavailable
agent = load_agent_profile(project_root, "coder")
# discard session at operation end
```

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — mars subprocess integration, resolve vs
list asymmetry, `MarsResultCache` scoping contract, and fallback to merged JSON

## Related

- `../launch/compiler.py` — consumes `AliasEntry` and `AgentProfile` from this layer
- `../config/settings.py` — config defaults fed into launch compiler
- `mars-agents` repo (sibling) — source of `.mars/agents/` and `.mars/models-merged.json`
