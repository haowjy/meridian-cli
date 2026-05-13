# Agent Profiles

Agent profiles are markdown files that tell Meridian how to launch and configure a specific agent: which model to use, which harness to run it on, which skills to inject, and what policies to apply when the selected model changes.

## Location

Meridian reads agent profiles from `.mars/agents/*.md`. This directory is populated by `meridian mars sync` — do not edit it directly.

```
.mars/
  agents/
    coder.md
    reviewer.md
    ...
```

To list installed profiles:

```bash
meridian mars list        # grouped by mode (primary / subagent)
meridian mars list --json # machine-readable
```

## Format

Each profile is a markdown file with YAML frontmatter:

```markdown
---
name: reviewer
description: >
  Reviews changes for correctness, regressions, and design alignment.
model: claude-sonnet
mode: subagent
skills:
  - meridian-spawn
---

Read-only review agent. Reports findings with severity, does not edit.
```

The frontmatter controls Meridian's routing and policy behavior. The markdown body is the agent's system prompt — it replaces Claude's default system prompt when passed via `--agent`.

## Frontmatter Fields

| Field | Type | Default | Purpose |
|---|---|---|---|
| `name` | str | filename stem | Profile identifier used with `-a NAME` |
| `description` | str | `""` | One-line description shown in `mars list` |
| `model` | str | — | Default model alias or ID (e.g. `claude-sonnet`, `gpt-5.3-codex`) |
| `harness` | str | — | Force a specific harness (`claude`, `codex`, `opencode`) |
| `mode` | str | `subagent` | `primary` or `subagent` — controls listing grouping |
| `model-invocable` | bool | `true` | `false` hides agent from model-facing inventory prompt; explicit `-a NAME` invocation still works |
| `skills` | list[str] | `[]` | Skill names to inject into the system prompt |
| `tools` | list[str] | `[]` | Tool names to allow |
| `disallowed-tools` | list[str] | `[]` | Tool names to block |
| `mcp-tools` | list[str] | `[]` | MCP tool names to enable |
| `sandbox` | str | — | Sandbox level override |
| `effort` | str | — | Effort level (`low`, `medium`, `high`) |
| `approval` | str | — | Approval mode (`default`, `confirm`, `auto`, `yolo`) |
| `autocompact` | int | — | Compaction percentage threshold |
| `timeout` | int | — | Spawn timeout in seconds |
| `model-policies` | list | `[]` | Per-model override rules (see below) |
| `models` | mapping | `{}` | Legacy per-model effort/autocompact overrides (deprecated) |

## `mode`

Controls how agents are grouped in `meridian mars list` and in the startup system prompt.

```yaml
mode: primary    # top-level orchestrators, primary sessions
mode: subagent   # spawned workers (default)
```

`meridian mars list` output:

```
AGENTS
- design-lead: Heavy design with research and adversarial review | Model: claude-opus-4-6
- product-manager: Dev workflow entry point | Model: claude-opus-4-6

## Subagent
- coder: Implementation tasks | Model: gpt55 | Fan-out: gpt55, codex
- reviewer: Adversarial review | Model: gpt-5.4 | Fan-out: gpt, opus
```

## `model-policies`

Per-model override rules applied when the resolved model matches a selector. Use this when one profile should behave differently depending on which model runs it — for example, setting higher effort for a weaker model, or forcing a specific harness for a closed-source model.

```yaml
model-policies:
  - match:
      model: anthropic/claude-sonnet-4-5
    override:
      harness: claude
      effort: high

  - match:
      alias: gpt5
    override:
      autocompact: 20

  - match:
      model-glob: "openai/*"
    override:
      sandbox: strict
```

### Match selectors

| Selector | Matches on | Example |
|---|---|---|
| `model` | Exact canonical model ID | `model: anthropic/claude-sonnet-4-5` |
| `alias` | Exact alias token used to select the model | `alias: sonnet` |
| `model-glob` | Glob pattern against canonical model ID | `model-glob: "openai/*"` |

Matching priority: `model` (most specific) > `alias` > `model-glob` (least specific). If two rules tie at the same specificity, launch fails with an ambiguity error.

### Override keys

Scalar overrides in `override:` accept the same keys as profile-level frontmatter:

`harness`, `sandbox`, `approval`, `effort`, `autocompact`, `timeout`

### Precedence

Model-policy overrides sit between explicit user flags and the profile's generic defaults:

```
CLI flag / ENV var  >  config overlay  >  model-policies match  >  profile defaults  >  config  >  alias defaults
```

Config overlays (`[agents.<name>]` in `meridian.toml` or `meridian.local.toml`) can replace the agent policy list (including replacing it with an empty list) per-project without editing the profile. See [configuration.md — Agent Runtime Overrides](configuration.md#agent-runtime-overrides).

## `fallback-order` in `model-policies`

Harness-availability fallback candidates are now declared directly in
`model-policies` with `fallback-order` (positive integer). Meridian sorts
candidates by ascending `fallback-order` and selects the first candidate whose
harness is available.

```yaml
model: claude-sonnet
model-policies:
  - match: { alias: gpt5 }
    fallback-order: 1
    override: { effort: medium }
  - match: { alias: codex }
    fallback-order: 2
    override: { effort: medium }
```

Fallback only activates when:

- the head candidate's harness is unavailable, **and**
- the user did not explicitly set a model with `-m` / `MERIDIAN_MODEL`

Migration: replace legacy `fanout` entries with `model-policies` rules that set
`fallback-order`.

## Skills and Skill Variants

When Meridian launches an agent, it loads the skills listed in `skills:` and injects them into the system prompt. Skills are read from `.mars/skills/`.

```yaml
skills:
  - meridian-spawn
  - shared-workspace
```

**Variant selection.** Skills can ship harness- or model-specific body overrides in a `variants/` subdirectory. Meridian selects the best matching variant at launch time using a 4-step specificity ladder:

1. `variants/<harness>/<model-alias>/SKILL.md` — model alias + harness
2. `variants/<harness>/<model-canonical-id>/SKILL.md` — canonical model ID + harness
3. `variants/<harness>/SKILL.md` — harness level only
4. Base `SKILL.md` — default

The base skill's frontmatter metadata is always authoritative; a variant only replaces the instruction body. Variant selection is transparent — the profile doesn't need to declare which variants a skill supports.

See [mars docs: skill-compilation.md](https://github.com/meridian-flow/mars-agents/blob/main/docs/config/skill-compilation.md) for the skill authoring format.

## Agent Listing

`meridian mars list` renders installed profiles grouped by `mode`:

```
## Primary
- my-orchestrator: Main orchestrator | Model: claude-opus-4-6

## Subagent
- coder: Implementation tasks | Model: gpt55 | Fan-out: gpt55, codex
- reviewer: Adversarial review | Model: gpt-5.4 | Fan-out: gpt, opus
```

Each line shows: name, description, default model, and fallback-chain aliases
(deduplicated by resolved model ID).

## Harness Availability Fallback

When a spawn is launched with an agent profile and the current head candidate's harness is unavailable — whether that is the profile's base model or a harness rerouted by a matched `model-policies` rule — Meridian walks the candidate chain and selects the next available alternative without failing:

1. Compile the base launch candidate from normal precedence (profile/config/user inputs).
2. Apply the active `model-policies` list to that base candidate. When a rule matches, its override becomes the new head. The original base candidate is retained as the next availability fallback in the chain.
3. Append `model-policies` rules that set `fallback-order`, sorted ascending.
4. Walk the resulting ordered candidate chain (policy-rerouted head → demoted base → fallback-order chain) and pick the first candidate whose harness is available.
5. If nothing resolves, fail with a clear error naming the unavailable harness.

`model-policies` participate as candidate transforms on top of the base launch candidate. They are not a separate token-discovery list outside the chain.

This means a profile like:

```yaml
model: claude-sonnet
model-policies:
  - match: { alias: gpt5 }
    fallback-order: 1
    override: { effort: medium }
```

...works on a machine with only Codex installed — it silently routes to `gpt5` rather than erroring.

## Legacy `models:` Field

The `models:` mapping was an earlier mechanism for per-model `effort` and `autocompact` overrides. It is still accepted but deprecated in favor of `model-policies:`.

```yaml
# deprecated — use model-policies instead
models:
  claude-sonnet:
    effort: high
  gpt5:
    autocompact: 20
```

A deprecation warning is logged when `models:` is present without
`model-policies:`.

## Example Profiles

### Minimal subagent

```markdown
---
name: summarizer
description: Summarizes documents concisely.
model: claude-haiku
---

You summarize documents. Be concise. Return only the summary.
```

### Multi-harness agent with model policies

```markdown
---
name: coder
description: Implementation tasks for backend, frontend, CLI, and infrastructure.
model: gpt55
model-policies:
  - match:
      alias: gpt55
    fallback-order: 1
    override:
      effort: medium
  - match:
      alias: codex
    fallback-order: 2
    override:
      effort: medium
  - match:
      model-glob: "anthropic/*"
    override:
      harness: claude
      effort: high
skills:
  - meridian-spawn
mode: subagent
---

You implement features. Pick over @frontend-coder when functional correctness is the goal.
```

### Primary orchestrator

```markdown
---
name: product-manager
description: Dev workflow entry point. Owns intent capture, scope sizing, and plan review.
model: claude-opus-4-6
mode: primary
---

You are the dev workflow entry point. Capture requirements, size scope, approve designs, review plans.
```
