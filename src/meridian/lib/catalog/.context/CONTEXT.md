# lib/catalog/ — Context

Alias resolution, agent profile parsing, and skill loading. The catalog layer
translates symbolic names into concrete execution parameters before any process
launches.

## Contracts

### Zero Builtin Aliases

Meridian defines no alias-to-model-id mappings internally. All aliases come from
mars packages (`.mars/models-merged.json`) and consumer config (`mars.toml [models]`).
Do not add hardcoded alias definitions to this module.

### Mars Subprocess Integration

**`run_mars_models_resolve(name, project_root)`** — raises `RuntimeError` when mars
is unavailable or broken. Returns `None` only when the alias is unknown (mars exit
code 1). Mars is always bundled with meridian, so unavailability is a hard error,
not a soft fallback.

**`run_mars_models_list(project_root)`** — returns `None` silently when mars is
unavailable (used for listing, not resolution). Falls back to reading
`.mars/models-merged.json` directly. This asymmetry is intentional: resolution must
succeed or fail loudly; listing can degrade gracefully.

**Timeout:** 60 seconds for all mars subprocess calls. Mars may do a cold
`models.dev` fetch on first boot; 60s leaves headroom for slow DNS, disk, and startup.

### MarsResultCache Scoping

`MarsResultCache` is per-operation, not module-global. Create it inside
`CatalogSession.__init__` or pass an existing one. It caches resolve, list, and
list-all results for the duration of one CLI invocation.

Caching transient `None` results is intentional — if mars returns "unknown alias"
once, it will return the same for the same input within the operation. Don't retry.

**Do not make `MarsResultCache` a module-level singleton** — it would cache across
separate CLI invocations and prevent alias updates from being picked up.

### Resolution Pipeline

`resolve_model(name_or_alias, project_root)` in `models.py`:

1. Call `mars models resolve <name> --json` (via `cached_mars_models_resolve`)
2. If mars returns a result → return `AliasEntry` from it
3. If mars returns `None` (unknown alias) → search `mars models list --all` for exact model ID match
4. If found in all-models list → return `AliasEntry` with empty alias (direct model ID passthrough)
5. If not found → return `AliasEntry` with the input as model ID and unresolved harness

### Agent Profile Model-Policy Parsing Rules (`agent.py`)

**`models:`** — raises `ValueError` at parse time. Removed; use `model-policies:` for
per-model overrides.

**`fanout:`** — raises `ValueError` at parse time. Not supported; profiles express
fallback candidates via ordered `model-policies` list rules instead.

**`fallback-order:`** in a `model-policies` rule — raises `ValueError`. Fallback
order is implicit from rule list position; remove this key.

**`no-fallback: true`** — opt-out on individual `model-policies` rules. Rules with
this set are excluded from harness-availability fallback. `model-glob` rules are
always `no_fallback = True`, hardcoded in `_parse_model_policies()` regardless of
what the profile declares.

**First-match wins.** Duplicate rules with the same selector are silently unreachable —
no parse error. The first matching rule in list order wins.

### AliasEntry

`AliasEntry.harness` property: returns `resolved_harness` if mars provided it;
raises `ValueError` otherwise. Missing harness is a mars-resolution bug, not a case
for pattern guessing. Callers should always use `.harness` (the property), not
`.resolved_harness` (the raw field).

`AliasEntry.mars_provided_harness` exposes the raw mars-provided value for
diagnostic display — do not use it for routing decisions.

### AgentProfile.model_invocable

`AgentProfile.model_invocable: bool = True` — controls whether an agent appears in
the model-facing inventory prompt. Rules:

- **Default is `True`** — profiles that omit `model-invocable` are visible to models.
  This preserves backward compatibility for all existing profiles.
- **YAML native booleans only** — `true` / `false` in frontmatter. Non-boolean values
  (strings, integers) silently default to `True`; they do not raise.
- **Informational metadata only** — `model_invocable` is not checked by
  `load_agent_profile()` or `scan_agent_profiles()`. Those remain neutral scanners
  that return all profiles regardless of this field.
- **Visibility filtering is the caller's responsibility.** The catalog layer does not
  filter. Mars renders harness-aware agent inventory in the launch-bundle
  `prompt_surface.inventory_prompt`; meridian consumes that string verbatim.

Do not add `model_invocable` checks to catalog scanning or loading functions —
consumers that need all profiles (CLI listing, explicit `-a <name>` resolution)
must not be affected by this flag.

## Fallback to `.mars/models-merged.json`

When the mars binary is unavailable, `load_mars_aliases()` falls back to reading
`.mars/models-merged.json` directly. This file contains only pinned aliases (those
with an explicit `model` key); auto-resolve aliases require mars to expand. Pinned
aliases resolve correctly from the file; auto-resolve aliases are skipped.

## Related KB

- [KB: Model Resolution](../../../../../../../../.meridian/git/meridian-flow-docs/kb/concepts/model-resolution/overview.md) — full resolution pipeline design
