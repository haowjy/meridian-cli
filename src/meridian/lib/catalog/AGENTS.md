# lib/catalog/

Agent profile loading, skill parsing, and model alias resolution. All alias
definitions come from mars packages — Meridian has zero builtin alias definitions.

## Entry Points

- `catalog_session.py` — `CatalogSession`: per-operation facade for catalog lookups
- `model_aliases.py` — `AliasEntry`, `MarsResultCache`, mars subprocess integration
- `models.py` — `resolve_model()`, `load_merged_aliases()`: compatibility exports
- `agent.py` — `AgentProfile`, agent frontmatter parsing for `.mars/agents/*.md`
- `skill.py` — skill YAML/markdown parsing for `.mars/skills/*/SKILL.md`
- `bootstrap.py` — harness bootstrap initialization
- `model_policy.py` — `pattern_fallback_harness()`: harness inference from model ID patterns

## Usage Pattern

```python
session = CatalogSession(project_root)
alias_entry = session.resolve_model("gpt55")   # raises RuntimeError if mars unavailable
agent = load_agent_profile(project_root, "coder")
```

Create one `CatalogSession` per launch operation and discard at operation end.
Do not cache sessions across operations — the mars result cache is scoped to one
operation lifetime.

## Depth

See [.context/CONTEXT.md](.context/CONTEXT.md) for:
- Mars subprocess integration and fallback to `.mars/models-merged.json`
- Why `run_mars_models_resolve` raises on mars unavailability (unlike `list` which returns None)
- MarsResultCache scoping contract
- Harness inference from model ID patterns (`pattern_fallback_harness`)

## Related

- `../config/settings.py` — config defaults fed into launch compiler
- `../launch/compiler.py` — consumes `AliasEntry` and `AgentProfile` from this layer
- `mars-agents` repo (sibling) — source of `.mars/agents/` and `.mars/models-merged.json`
