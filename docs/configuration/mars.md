# Mars configuration and agent runtime

Use Mars config for package materialization, project routing defaults, model
aliases, and per-agent runtime policy. The generated `.mars/agents/` profiles
are build output; do not edit them directly.

## User packages and bare directories

User packages live in the ordinary Mars project at `~/.meridian/mars`. Populate
and compile it explicitly:

```bash
meridian mars --user add haowjy/meridian-base
meridian mars --user sync
```

In a directory without a project `mars.toml`, Meridian uses this user root for
agents, skills, aliases, models, and launch defaults. With no `targets` configured,
the user root permits every installed harness. A project Mars root remains
authoritative for routing config and package content takes precedence over user
content.

Inside a project Mars root, user packages are fallback content for agents and
skills only. Model aliases still resolve from the project's Mars config. If a
user agent depends on an alias that the project does not define, add its package
to the project with `meridian mars add <source>` or pass `--model`.
`meridian config show` and `meridian context` report `mars_fallback` as `active`
when this fallback is available, `unsupported` when the installed Mars lacks it,
and `n/a` when the user root is already primary.

Package mutations without `--user` require a project Mars root; so do `migrate` (except
`migrate --check`, which only reports) and `provide`. In a bare
directory they fail before Mars can create a marker. `meridian mars init` is the
explicit exception: it writes only `mars.toml`; the next Meridian runtime write
mints the committed `[project] id` in `meridian.toml`.

## Project routing defaults

```toml
[settings]
targets = [".claude", ".codex", ".opencode", ".pi", ".cursor"]
default_model = "gptmini"
default_harness = "codex"
models_cache_ttl_hours = 24
```

`targets` is also launch permission: Mars may only try those harnesses, whether
or not other CLIs are installed or logged in. Empty `targets = []` is closed.
`default_model` and `default_harness` provide project routing defaults used when
no stronger CLI/env/profile setting is present; `default_harness` cannot
resurrect a rejected route.

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

`include_fanout` (default `false`) controls whether fan-out agents — agents that qualify
for a harness only through their `model-policies` rules, not their own `harness:` or
`model:` — are materialized as native copies. The profile `fanout` key is gone from
schema v1; `[settings.meridian.fanout]` below names them instead.
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

Headless spawns are allowed on every harness unless a project opts out.
Use `[spawn].deny_headless_harnesses` when a project should not launch
headless subagents through specific harnesses. This applies to
`meridian spawn`, including `meridian spawn -a <agent>`. Exclusions reach Mars
before route selection; excluded harnesses are not auth-probed.

```toml
[spawn]
deny_headless_harnesses = ["claude"]
```

With that setting, a Claude primary session can still run. Spawns try permitted
routes and declared model backups instead of selecting Claude and rejecting it
afterward. An explicit Claude harness pin fails; it cannot override the exclusion.
To completely avoid Claude-side delegation, combine it with no Claude `agent_copy`:

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
override = { effort = "medium", sandbox = "workspace-write" }

[[agents.tech-lead.model-policies]]
match = { alias = "codex" }
override = { effort = "medium" }
```

Legacy `[agents]` sections in `meridian.toml` / `meridian.local.toml` are
unsupported and fail with migration guidance.

### Supported fields

Scalar fields: `model`, `harness`, `effort`, `approval` (`default`, `confirm`, `auto`,
`never`), `sandbox` (`default`, `read-only`, `workspace-write`, `danger-full-access`),
`network` (`allow`, `deny`, `allowlist`). Mars checks `effort`, `approval`, `sandbox` and
`network` when it loads `mars.toml`, so a bad value (for example `approval = "yolo"`) fails
every `meridian mars` command and launch. Compaction is not an overlay field: set
`autocompact` and `autocompact_pct` on `[models.<alias>]`
([upgrade note](../upgrading.md#if-you-write-marstoml-overlays)). A `model-policies` override
sets only `effort`, `sandbox`, `approval` and `network`.

**`timeout` is not supported in agent overlays.** Timeout continues to resolve
through CLI/env/profile/Meridian-config paths.

### Model-policy rules

Agent overlay rules use the same `match`, `override`, and fallback semantics as profile `model-policies`. See [agent-profiles.md](../agent-profiles.md#model-policies).

Overlay rules are prepended before profile rules to form one effective
ordered `model-policies` list. Matching uses first-match-wins by list order.
Duplicate matches are valid; the earlier rule in the combined list wins.

`approval`, `sandbox`, and `network` overlays take effect, with the per-harness limits in
[Permissions by harness](../agent-profiles.md#permissions-by-harness). A
`[agents.<name>.tools]` table merges into the profile's tool policy: plain `allowed` and
`disallowed` lists add to it, and `{ replace = [...] }` clears that action's lower rules.
The merged rules reach both native agent files and the launch bundle, so Meridian launches
with them. A list-valued `tools` key under `[agents.<name>]` is a parse error.

### Three-state model-policy semantics

- **Key absent**: Inherits model-policies from the agent profile
- **Empty array** (`model-policies = []`): Prepends nothing; effective behavior is profile rules only.
- **Non-empty array**: Prepends overlay rules before profile rules (not merged by match key).

### Precedence

Per-field, strongest to weakest:
1. CLI flags (`-m`, `--effort`, etc.)
2. Environment variables (`MERIDIAN_MODEL`, `MERIDIAN_EFFORT`)
3. Agent overlay scalar fields (`[agents.<name>]` `effort`, `approval`, `sandbox`, `network`)
4. Matched model-policy rule (from combined overlay+profile list, first-match-wins)
5. Agent profile defaults
6. Config-level defaults
7. Model alias defaults

Local Mars overrides win over project Mars config for overlay values.

### Observability

- `meridian mars export --json` shows the compiled package/materialization plan.
- `meridian mars models aliases --json` shows static aliases;
  `meridian mars models catalog --json` shows raw model metadata.
  `meridian mars models list` is a curated human view, and
  `resolve ALIAS` resolves an individual alias.
- `meridian spawn --dry-run` reflects overlay-aware routing and provenance
