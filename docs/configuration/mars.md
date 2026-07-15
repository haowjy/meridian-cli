# Mars configuration and agent runtime

Use Mars config for package materialization, project routing defaults, model
aliases, and per-agent runtime policy. The generated `.mars/agents/` profiles
are build output; do not edit them directly.

## Project routing defaults

```toml
[settings]
targets = [".claude", ".codex", ".opencode", ".pi", ".cursor"]
default_model = "gptmini"
default_harness = "codex"
models_cache_ttl_hours = 24
```

`targets` controls harness-native materialization directories. `default_model`
and `default_harness` provide project routing defaults used when no stronger
CLI/env/profile setting is present.

## Native agent copies

Meridian normally delegates through `meridian spawn` and reads agents from
`.mars/agents/`. Meridian CLI processes default `MERIDIAN_MANAGED=1` in their environment,
while preserving an explicit outer override. Mars therefore suppresses
harness-native agent copies by default so `.claude/agents/` and similar
directories do not compete with Meridian routing.

Use `[settings.meridian.agent_copy]` when you intentionally want selected harness-native
copies under managed mode:

```toml
[settings]
targets = [".claude", ".codex"]
agent_emission = "never"  # optional; agent_copy can still emit selected copies

[settings.meridian.agent_copy]
harnesses = ["claude"]
include_fanout = false          # default false: skip fan-out (sub-delegate) agents
```

Each listed harness must also have an effective managed target (for Claude,
`.claude`, from `settings.targets` or legacy `managed_root` when `targets` is
unset). Mars then materializes only qualifying agents for that harness;
`.mars/agents/` remains the canonical Meridian spawn source.
`agent_emission = "always"` is broader and emits all native agent copies.

`include_fanout` (default `false`) controls whether fan-out agents — sub-delegates
listed in an agent profile's `fanout` field — are materialized as native copies.
When `include_fanout = false`, fan-out agents are skipped even if they qualify for
the selected harness.

## Fan-out agent routing

`[settings.meridian.fanout]` is a peer table to `[settings.meridian.agent_copy]`,
not nested under it:

```toml
[settings.meridian.fanout]
agents = ["reviewer", "browser-prober", "prober"]
```

`agents` is an allowlist of fan-out agent names. Each listed agent:

1. **Native-copy emission** — qualifies for harness-native materialization even
   when `include_fanout = false`. The agent must still qualify for the harness via
   its model policies; `agents` is a scope filter, not a force-emit.
2. **Dual inventory listing** — appears in both the Meridian `## Subagent`
   section (primary route: `meridian spawn -a <name>`) and the native harness
   section (escalation route: `Agent({subagent_type: "<name>"})` for Claude).

The former `agent_copy` fan-out allowlist key is removed. If present in
`mars.toml`, mars emits a migration warning pointing to
`[settings.meridian.fanout].agents` and ignores the old value.

For Claude launches, Meridian uses the same boundary for native `Agent()`
delegation. Generic Claude `Agent` is allowed only when Mars has Claude
`agent_copy` enabled and `.claude` is an effective managed target; otherwise
the launch denies native `Agent` and guides delegation back to `meridian spawn`.
Claude built-in
agents (`Explore`, `Plan`, `General-purpose` / `general-purpose`) stay denied.

## Deny headless spawn harnesses

Use `[spawn].deny_headless_harnesses` when a project should not launch
headless subagents through specific harnesses. This applies to
`meridian spawn`, including `meridian spawn -a <agent>` after the agent/model
resolves to a harness.

```toml
[spawn]
deny_headless_harnesses = ["claude"]
```

With that setting, a Claude primary session can still run, but headless Claude
spawns fail before launch. To completely avoid Claude-side delegation, combine
it with no Claude `agent_copy`:

```toml
[settings.meridian.agent_copy]
harnesses = []
```

## Model aliases

Project-local aliases live under `[models.<alias>]`:

```toml
[models.composer]
harness = "cursor"
model = "composer-2.5"
provider = "cursor"
description = "Cursor's native coding model. Fast, strong tool use."
```

Prefer model aliases as the user-facing interface. When invoking Meridian, pass
the alias as-is; Mars resolves it at launch time.

## Agent runtime overlays

Override agent runtime policy per project without editing generated
`.mars/agents/` profiles. Agent overlays now live in Mars config:

```toml
[agents.tech-lead]
model = "gpt55"
effort = "medium"
approval = "auto"

[[agents.tech-lead.model-policies]]
match = { model-glob = "gpt*" }
override = { effort = "medium", autocompact_pct = 40 }

[[agents.tech-lead.model-policies]]
match = { alias = "codex" }
override = { effort = "medium" }
```

Legacy `[agents]` sections in `meridian.toml` / `meridian.local.toml` are
unsupported and fail with migration guidance.

### Supported fields

Scalar fields: `model`, `harness`, `effort`, `approval`, `sandbox`, `autocompact`, `autocompact_pct`.

**`timeout` is not supported in agent overlays.** Timeout continues to resolve
through CLI/env/profile/Meridian-config paths.

### Model-policy rules

Agent overlay rules use the same `match`, `override`, and fallback semantics as profile `model-policies`. See [agent-profiles.md](../agent-profiles.md#model-policies).

Overlay rules are prepended before profile rules to form one effective
ordered `model-policies` list. Matching uses first-match-wins by list order.
Duplicate matches are valid; the earlier rule in the combined list wins.

List/tool override keys (`skills`, `tools`, `disallowed-tools`, `mcp-tools`) are rejected with a warning in agent overlays.

### Three-state model-policy semantics

- **Key absent**: Inherits model-policies from the agent profile
- **Empty array** (`model-policies = []`): Prepends nothing; effective behavior is profile rules only.
- **Non-empty array**: Prepends overlay rules before profile rules (not merged by match key).

### Precedence

Per-field, strongest to weakest:
1. CLI flags (`-m`, `--effort`, etc.)
2. Environment variables (`MERIDIAN_MODEL`, `MERIDIAN_EFFORT`)
3. Matched model-policy rule (from combined overlay+profile list, first-match-wins)
4. Agent overlay generic defaults
5. Agent profile defaults
6. Config-level defaults
7. Model alias defaults

Local Mars overrides win over project Mars config for overlay values.

### Observability

- `meridian mars export --json` shows the compiled package/materialization plan.
- `meridian mars models list` and `meridian mars models resolve ALIAS` show model aliases.
- `meridian spawn --dry-run` reflects overlay-aware routing and provenance
