# Agent Profiles

Agent profiles are markdown files that tell Meridian how to launch and configure a specific agent: which model to use, which harness to run it on, which skills it loads, and what policies to apply when the selected model changes.

## Location

Meridian reads agent profiles from `.mars/agents/*.md`. This directory is
compiled by `meridian mars sync` from Mars packages — do not edit it directly.
Change source packages or use Mars config overlays instead.

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
| `harness` | str | — | Force a specific harness: `claude`, `codex`, `opencode`, `cursor`, or `pi` |
| `mode` | str | `subagent` | `primary` or `subagent` — controls listing grouping |
| `model-invocable` | bool | `true` | `false` hides agent from model-facing inventory prompt; explicit `-a NAME` invocation still works. Skills have their own `model-invocable`; see [Skills](#skills-and-skill-variants) |
| `skills` | list[str] or `{load, available}` | `[]` | Skills the agent loads into its system prompt; the mapping form also lists skills it may load itself. See [Skills](#skills-and-skill-variants) |
| `tools` | map or list[str] | — | Per-tool rules: `bash: allow`, `edit: deny`. A list allows each name. A scoped key such as `bash(rg *)` limits a rule to matching calls. `allow` and `deny` are the only values; `ask` is rejected. MCP grants are `mcp(server)` entries. What each harness does with the rules: [Permissions by harness](#permissions-by-harness) |
| `disallowed-tools` | list[str] | `[]` | Tools to deny, with the same names and scoped keys as `tools`. Equivalent to `deny` entries in the `tools` map |
| `sandbox` | str | — | `default`, `read-only`, `workspace-write`, or `danger-full-access`. Applied by Codex and Cursor only: [Permissions by harness](#permissions-by-harness) |
| `effort` | str | — | Effort level (`low`, `medium`, `high`) |
| `approval` | str | — | When the harness stops to ask: `default`, `confirm`, `auto`, or `never`. `yolo` is a deprecated spelling of `never`. See [Permissions by harness](#permissions-by-harness) |
| `autocompact` | int | — | Compaction percentage threshold |
| `timeout` | int | — | Spawn timeout in seconds |
| `resident-rearm-budget` | int | — | Maximum resident deadline extensions; omitted means unlimited |
| `model-policies` | list | `[]` | Per-model override rules (see below) |

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

  - match:
      model-glob: "openai/*"
    override:
      sandbox: read-only
```

### Match selectors

| Selector | Matches on | Example |
|---|---|---|
| `model` | Exact canonical model ID | `model: anthropic/claude-sonnet-4-5` |
| `alias` | Exact alias token used to select the model | `alias: sonnet` |
| `model-glob` | Glob pattern against canonical model ID | `model-glob: "openai/*"` |

Matching is first-match-wins by list order. No specificity ranking or ambiguity errors are applied. If multiple rules could match, the earliest rule wins.

### Override keys

Omit `override` or set `override: {}` when the rule only declares fallback order.
Unknown override keys are ignored by Meridian so newer Mars packages can remain
launchable on older Meridian versions.

Scalar overrides in `override:` accept these runtime keys:

`harness`, `sandbox`, `approval`, `effort`, `autocompact`, `autocompact_pct`, `timeout`

`resident-rearm-budget` is a top-level profile default, not a model-policy
override. The Mars launch-bundle schema does not currently expose a per-model
resident rearm budget.

### Precedence

Model-policy overrides sit between explicit user flags and the profile's generic defaults:

```
CLI flag / ENV var  >  Mars agent overlay  >  model-policies match  >  profile defaults  >  Meridian config  >  alias defaults
```

Agent overlays (`[agents.<name>]` in Mars config such as `mars.toml`) prepend
their `model-policies` rules before profile rules to form one effective ordered
list. Legacy `[agents]` sections in `meridian.toml` / `meridian.local.toml` are
unsupported. See [configuration.md — Mars-Owned Routing and Agent Runtime](configuration/mars.md#model-policies).

## Implicit Fallback Ordering in `model-policies`

Harness-availability fallback candidates are derived directly from
`model-policies` list order:

- rules with `match.alias` or `match.model` are fallback candidates, in the
  order they appear
- rules with `match.model-glob` are override-only (never fallback candidates)
- `no-fallback: true` opts a rule out of fallback candidacy

```yaml
model: claude-sonnet
model-policies:
  - match: { alias: gpt5 }
    override: { effort: medium }
  - match: { alias: codex }
    override: { effort: medium }
  - match: { model: openai/gpt-5.3-codex }
    no-fallback: true
    override: { autocompact: 20 }
```

Fallback only activates when:

- the head candidate's harness is unavailable, **and**
- the user did not explicitly set a model with `-m` / `MERIDIAN_MODEL`

Legacy `fallback-order` and `fanout` are rejected. Order `model-policies`
rules directly and add `no-fallback: true` for rules that should never be
considered for fallback.

## Skills and Skill Variants

`skills:` takes a flat list or a mapping:

```yaml
skills:
  - meridian-spawn
  - shared-workspace
```

```yaml
skills:
  load: [meridian-spawn, shared-workspace]
  available: [diagnose, probe]
```

A flat list means every skill is loaded. A name in both `load` and `available`
counts as loaded.

`shared-workspace` guidance should set the work item's source-edit
directory with `meridian work start ... --task-dir <path>` or
`meridian work task-dir <path>` (for example, pointing at a `git worktree add`
directory).

| How the agent gets the skill | What reaches the agent |
|---|---|
| `load` (or the flat list) | The skill's full text, in the system prompt after the profile body, on every launch |
| `--skills NAME` on `meridian spawn` or `meridian start` | Same as `load`, for that launch |
| `available` | The skill's name under `# Available Skills`, grouped by [type](https://github.com/meridian-flow/mars-agents/blob/main/docs/config/skill-compilation.md#type). The agent loads it itself through the harness's own skill mechanism, such as Claude Code's Skill tool |

**Known gap:** on OpenCode v2 and the `opencode run` path, the composed system
prompt, including loaded skills and the profile body, does not reach the model.
OpenCode v1 is unaffected.

**Loaded skills ignore the skill's `model-invocable`.** A skill's own
`model-invocable: false` only keeps it out of the harness's skill list, the list
a model browses to find skills by itself. A loaded skill still arrives in full.
So `model-invocable: false` plus `load` gives one agent a skill that no other
agent sees or loads on its own. The reverse pairing doesn't work: an `available`
skill with `model-invocable: false` is named in the listing, but the harness
won't load it.

Each loaded skill is headed `# Skill: /<name>` with a line saying its text is
already loaded and where its resource files are. An agent body can name a loaded
skill (`/solo-dev`) even though the skill won't appear in the harness's skill
list.

**Bundle interface.** Skill content comes from the Mars launch-bundle JSON, not
from Meridian reading `.mars/skills/` on disk. Mars owns the `.mars/` directory
schema, skill variant selection, and compilation:

```
Mars launch-bundle → skills.loaded[{name, skill_type, source_path, body}]
                   → skills.available[{name, skill_type, description, source_path}]
                   → skills.missing[name]
```

Meridian receives already-resolved content and:
- Renders type-group headers (`## Principle`, `## Guardrail`, etc.) for loaded skills
- Lists available skills by name, grouped by type, with a load nudge
- Unknown skill names produce a warning, not a failed launch

**Variant selection** happens inside Mars. If a skill has
`variants/<harness>/SKILL.md`, its body replaces the base body for that harness;
the base frontmatter still supplies metadata. Model-level
variants (`variants/<harness>/<model>/`) are not supported.

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
3. Append fallback-eligible `model-policies` rules in declared list order (rules matched by `alias` or `model`; skip `no-fallback: true`; `model-glob` remains override-only).
4. Walk the resulting ordered candidate chain (policy-rerouted head → demoted base → implicit list-order fallback chain) and pick the first candidate whose harness is available.
5. If nothing resolves, fail with a clear error naming the unavailable harness.

`model-policies` participate as candidate transforms on top of the base launch candidate. They are not a separate token-discovery list outside the chain.

This means a profile like:

```yaml
model: claude-sonnet
model-policies:
  - match: { alias: gpt5 }
    override: { effort: medium }
```

...works on a machine with only Codex installed — it silently routes to `gpt5` rather than erroring.

## Permissions by harness

A profile has three permission fields: `tools` (which tools the agent may call), `sandbox` (what the harness may touch on disk), and `approval` (when the harness stops to ask).

```yaml
tools:
  'bash(rg *)': allow     # scoped key: only matching shell commands
  edit: deny
sandbox: read-only
approval: never
```

A *spawn* is an agent Meridian launches with `meridian spawn`; a *primary* is the interactive session you start with `meridian` and work in yourself. Each harness applies only some of these fields. Meridian drops the rest without an error or a warning, so a profile that is read-only on one harness can be unrestricted on another. Read the row for the harness the profile runs on before relying on a field.

| Harness | `tools` | `sandbox` | `approval` |
|---|---|---|---|
| **Claude**, spawn and primary | `deny` removes the tool. `allow` only skips the prompt for it; tools you don't list stay available, whatever `*` or the list form says | No effect | `confirm` asks (permission mode `default`). `auto` approves file edits and asks for other tools. `never` skips every permission check. `default` leaves Claude's own default |
| **Codex**, spawn and primary | No effect, except `web: allow`, which turns on Codex web search | Applied. With `tools` set and `sandbox` unset, Codex infers one: `workspace-write` when `bash` or `edit` is allowed, `read-only` otherwise | `auto` lets Codex decide when to ask (`on-request`). `never` stops Codex asking; the sandbox still applies. `confirm` is the same as `auto`: Codex has no stricter mode |
| **OpenCode**, spawn | Applied. Denied tools are removed from the model's tool list. `*: deny` (and the list form) denies every tool the profile does not allow | No effect | No effect |
| **OpenCode**, primary | No effect: the TUI uses OpenCode's own permissions | No effect | No effect |
| **Pi**, spawn and primary | No effect | No effect | No effect |
| **Cursor**, spawn only | No effect | `read-only` turns Cursor's sandbox on. `workspace-write` and `danger-full-access` turn it off; Cursor's sandbox has no middle setting | `never` passes `--yolo`, `auto` passes `--force`. `default` and `confirm` pass nothing |

The same limits apply when `approval` or `sandbox` comes from `--approval`, `--sandbox`, `--yolo`, a [Mars agent overlay](configuration/mars.md#agent-runtime-overlays), or `[primary]` in [Meridian config](configuration.md) instead of the profile. `tools` comes only from the profile.

Things the table implies:

- **Denying `edit` does not make an agent read-only.** It removes the edit tools, and a shell command can still write a file. A sandbox stops that: `sandbox: read-only` on Codex and Cursor. Claude and OpenCode have no sandbox here, so deny `bash` too. Nothing restricts writes on Pi.
- **Mars names each tool for the harness.** `edit: deny` on Claude denies `Edit`, `Write` and `NotebookEdit`; `web: deny` denies `WebSearch` and `WebFetch`; `ask_user` becomes `AskUserQuestion`. A tool Mars has no name for on that harness shows as `unsupported` in `--dry-run` and is not enforced. On Claude, Meridian also denies Claude's own sub-agent tools (`Agent(Explore)`, `Agent(Plan)`, `Agent(General-purpose)`) on every launch, and the `agent` and `task` tools in spawns unless the profile allows them, so delegation goes through `meridian spawn`.
- **On Codex, `confirm` is no stricter than `auto`.** Codex 0.163 accepts only `on-request` and `never`, so both `confirm` and `auto` launch with `on-request`, where the model decides when to ask.
- **On Codex, set `sandbox` when you care which one applies.** With `tools` set and no `sandbox`, Codex infers `workspace-write` if `bash` or `edit` is allowed and `read-only` if neither is, which may not be what you meant.
- **A list is an allowlist.** `tools: [read, grep]` means exactly those tools, the same as `'*': deny` with `read` and `grep` allowed. Only OpenCode spawns enforce it today; `--dry-run` shows `* = deny` as `unsupported` on the others, where unlisted tools stay available.
- **A capability with only scoped allows is an allowlist for that capability.** With `'bash(rg *)': allow` and no `bash` rule, `rg` is the only command that may run, whatever `approval` says: `approval: never` means "don't prompt", not "allow anything". A `bash` rule of its own, or a `*` rule, decides every other command instead. No harness enforces this yet; `--dry-run` shows `tools: bash allowlist` as `unsupported`.
- **Platform denies hold under an allowlist.** Spawns still deny native sub-agent tools and Claude's built-in agents under `'*': deny` or a list.
- **OpenCode primaries ignore `tools`.** The same profile restricts an OpenCode spawn and not an OpenCode primary session.

`meridian spawn -a NAME --dry-run` and `meridian --dry-run -a NAME` print the command line Meridian builds, then a `Permissions:` section with one line per rule, platform rule, sandbox and approval value. Each line says whether the harness enforces it as written (`native`), only partly (`partial`), not at all (`unsupported`), or because a Meridian platform rule decides it (`platform`), and names the flag or setting it became. `--json` carries the same lines as `permission_report`, and a launched spawn's `projection-manifest.json` records them. OpenCode receives its rules in its environment instead of argv: `OPENCODE_PERMISSION` on OpenCode 1.x, `OPENCODE_CONFIG_CONTENT` on OpenCode 2.x; the `Permissions:` section shows what it gets.

### Who answers an approval request

A request arises when a tool call needs approval under the harness's approval mode, or, on OpenCode, when a rule says `ask`. Claude sends an `ask` rule as `allow`, and Codex and Pi do not enforce it.

A spawn has nobody to answer, so its requests are refused at once, never held and never passed to the parent agent (a model approving would defeat a rule written for a human). `meridian spawn show` lists the refused requests. Where the spawn's harness reports a request to Meridian, the refusal is recorded in the spawn's `permission_requests.jsonl`.

| Where | What happens |
|---|---|
| Codex or OpenCode spawn | Meridian refuses the request and records it in `permission_requests.jsonl`. The agent's tool call fails: OpenCode 2 tells the model why; Codex's protocol has no field for a reason, so the model sees `Rejected("rejected by user")` |
| Claude spawn | Claude blocks the call and returns an approval-required error to the agent. This applies to the agent's Meridian commands too: under `approval: confirm` a spawn can be refused its own `meridian spawn done` |
| Pi spawn | Pi has no approval prompts |
| Claude or OpenCode primary | The harness asks the user in its own TUI |
| Codex primary | Codex sends approval requests to Meridian instead of showing them in its TUI. Meridian records them and nothing answers, so the tool call does not run. Avoid profiles that need approvals on a Codex primary |
| Cursor | Unknown: not observed. Don't rely on approval prompts in Cursor spawns |

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
    override:
      effort: medium
  - match:
      alias: codex
    override:
      effort: medium
  - match:
      model-glob: "anthropic/*"
    no-fallback: true
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
