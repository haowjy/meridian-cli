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
| `mode` | str | `subagent` | `primary` or `subagent` — controls listing grouping and which lead-only Meridian guidance the prompt carries; see [`mode`](#mode) |
| `model-invocable` | bool | `true` | `false` hides agent from model-facing inventory prompt; explicit `-a NAME` invocation still works. Skills have their own `model-invocable`; see [Skills](#skills-and-skill-variants) |
| `skills` | list[str] or `{load, available}` | `[]` | Skills the agent loads into its system prompt; the mapping form also lists skills it may load itself. See [Skills](#skills-and-skill-variants) |
| `tools` | map | — | Per-tool rules: `bash: allow`, `edit: deny`, or `web: ask`. A map containing `'*': deny` is an allowlist. A scoped key such as `bash(rg *)` limits a rule to matching calls. MCP rules use `mcp(server)` or `mcp(server/tool)`. What each harness enforces: [Permissions by harness](#permissions-by-harness) |
| `sandbox` | str | — | `default`, `read-only`, `workspace-write`, or `danger-full-access`. Harness support differs: [Permissions by harness](#permissions-by-harness) |
| `network` | str | sandbox-derived | Whether shell commands may open connections: `allow`, `deny`, or `allowlist`. `network-allow` and `network-deny` hold exact hosts, IPs, or wildcard hosts. Unset denies under `read-only`/`workspace-write`, allows under `danger-full-access`, and leaves an unset/default sandbox harness-native. Built-in web tools remain under `tools: web`. See [Permissions by harness](#permissions-by-harness) |
| `network-allow` | list[str] | `[]` | Hosts permitted by `network: allowlist`. Plain lists add across policy layers; see the [merge rules](#permissions-by-harness) |
| `network-deny` | list[str] | `[]` | Hosts denied under `allow` or `allowlist`. Plain lists add and denies win; see the [merge rules](#permissions-by-harness) |
| `effort` | str | — | Effort level: `low`, `medium`, `high`, `xhigh`, or `max`. `none` is not supported yet |
| `approval` | str | — | When the harness stops to ask: `default`, `confirm`, `auto`, or `never`. Write `never`: schema v1 rejects `yolo` in a `mars.toml` overlay or an installed profile (`meridian mars migrate` rewrites authored profiles). Meridian's `--approval yolo`, `--yolo` and `MERIDIAN_APPROVAL=yolo` still mean `never`. See [Permissions by harness](#permissions-by-harness) |
| `resident-rearm-budget` | int | — | Maximum resident deadline extensions; omitted means unlimited. Meridian reads it from the installed profile; Mars reports it as an unknown field on every sync |
| `harness-overrides` | map | `{}` | Harness-native launch config selected for the resolved harness; see [Harness-native passthrough](#harness-native-passthrough) |
| `model-policies` | list | `[]` | Per-model override rules (see below) |

## `mode`

Controls how agents are grouped in `meridian mars list` and in the startup system prompt.
Primary-mode profiles also receive the lead-only bundled skill triggers and the
`meridian notify` capability, including when launched as a spawn; see
[Skills](#skills-and-skill-variants).

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

Per-model override rules applied when the resolved model matches a selector. Use this when one profile should behave differently depending on which model runs it — for example, setting higher effort for a weaker model, or a read-only sandbox for an unfamiliar one.

```yaml
model-policies:
  - match:
      model: anthropic/claude-sonnet-4-5
    override:
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

An `override:` sets only these keys:

`sandbox`, `network`, `approval`, `effort`

Mars fails to load a `mars.toml` rule with any other key, and `meridian mars migrate`
deletes `harness`, `autocompact` and `autocompact_pct` from authored profile rules.
Set compaction on the model alias ([Compaction](#compaction-by-harness)) and `harness`
on the profile or the alias.

`resident-rearm-budget` is a top-level profile default, not a model-policy
override. The Mars launch-bundle schema does not currently expose a per-model
resident rearm budget.

Spawn timeout is not an agent-profile field. Set the runtime ceiling in minutes
with `--timeout`, `MERIDIAN_TIMEOUT`, or Meridian's `primary.timeout` config.

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
    override: { effort: high }
```

Fallback only activates when:

- the head candidate's harness is unavailable, **and**
- the user did not explicitly set a model with `-m` / `MERIDIAN_MODEL`

Profile `fallback-order` and `fanout` are gone in schema v1: sync drops them
from pre-v1 content, and a `schema = 1` package fails on them. Order
`model-policies` rules directly and add `no-fallback: true` for rules that
should never be considered for fallback; name fan-out agents in
[`[settings.meridian.fanout]`](configuration/mars.md#fan-out-agent-routing). See
[upgrading](upgrading.md#if-you-write-profiles-or-skills).

## Compaction by harness

A profile has no compaction field. Mars takes `autocompact` (an absolute token value) and
`autocompact_pct` (a percentage) from the model alias, in `[models.<alias>]`; the primary
session also reads `primary.autocompact` and `primary.autocompact_pct` from
[`meridian.toml`](configuration.md), and `--autocompact` / `--autocompact-pct` override both.
The two are separate controls, not alternate spellings of the same number. An alias that
sets its own value wins over `primary.autocompact`. What each harness does with them:

| Harness | `autocompact` | `autocompact_pct` |
|---|---|---|
| **Claude** | `--autocompact <tokens>` for values from 100000 through 1000000 | `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=<percent>` |
| **Codex** | `-c model_auto_compact_token_limit=<tokens>` | `-c model_post_turn_compact_threshold_percent=<percent>` |
| **OpenCode**, **Pi**, **Cursor** | Not applied | Not applied |

Claude applies the percentage to its autocompact window, so both fields can be
set together. A Claude token value outside its native 100000–1000000 range is
not passed to the CLI. `--dry-run` reports every applied value and every
unsupported value or harness.

## Harness-native passthrough

Mars selects `harness-overrides.<resolved-harness>` and passes the block to Meridian
without interpreting it. The selected block is applied as written and can change anything
in Codex's config, including providers and MCP server commands. Installing a package that
carries harness overrides is therefore installing code you trust. Blocks in already-installed
packages that older Meridian builds left inert now take effect. Codex flattens nested mappings
to dotted config keys and emits TOML-compatible scalar, array, and inline-table values as
`-c` overrides. The following native spelling for network access is deprecated; use the
profile's `network: allow` field instead:

```yaml
harness-overrides:
  codex:
    sandbox_workspace_write:
      network_access: true
```

Meridian-owned Codex settings win: native entries that collide with its model, effort,
sandbox, approval, writable-root, tool-switch, agent, feature, or notification config are
skipped. For this compatibility release only,
`sandbox_workspace_write.network_access` is an exception: Meridian warns, applies a
well-formed boolean after the profile projection, and that native value wins. It becomes
reserved and skipped like `writable_roots` in the following release. A non-boolean,
unencodable, or unaddressable value is warned, reported as ignored, and does not suppress
the portable profile projection. Meridian does not synthesize Codex MCP config,
so `mcp_servers.*` entries pass through like other native config. A key segment containing
`.`, `=`, whitespace, or quotes cannot be addressed by Codex's `-c` syntax and is reported
as unsupported and ignored. `--dry-run` shows each applied or skipped key, for example
`[native] native_config: sandbox_workspace_write.network_access = true -> -c sandbox_workspace_write.network_access=true`.
Claude, OpenCode, Pi, and Cursor do not apply native config; their report lists the key
names as `[unsupported] native_config: ... -> not applied for <Harness>`.

The selected harness block is the config tree. A key named `native-config` inside it is a
literal root key and is flattened like any other key; Meridian does not unwrap it.

## Skills and Skill Variants

Meridian's own operating guidance ships with the binary. When the Mars Meridian
runs has `mars provide` (Mars 0.20 or later), it is provided into the resolved Mars
root before launch; with an older Mars, Meridian reads its built-in copies. Every launch receives a compact `# Meridian
skills` trigger list generated from the binary's bundled descriptions, except for
a skill Mars loaded with non-blank content into the prompt. The agent reads a
trigger only when needed with
`meridian skills NAME [RESOURCE-PATH]`, which prints a skill under the same header a
loaded skill gets, naming its installed folder; `meridian skills list` shows the same
catalog, with structured entries under `--json`. These binary-owned skills may name
the concrete Meridian commands and flags they explain. Bundled skill frontmatter may
set `metadata.mode: primary`; its trigger then appears only when the agent profile declares
`mode: primary`, even when that profile is launched as a spawn. Handoff, notification,
work-item, privilege-escalation, and prompt-authoring guidance use this lead-only gate,
while session mining remains visible to every profile. The capabilities block likewise
shows `meridian notify` only to primary-mode profiles. Bundled skills are named
`escalation__meridian`, `handoff__meridian`, `notify__meridian`,
`prompt-authoring__meridian`, `sessions__meridian`, and `work__meridian`.
`prompt-authoring__meridian` covers writing agent profiles and skills in `.mars-src/`
without a prompt package. The
`name__meridian` suffix follows Mars's `name__source` collision spelling and stays
stable across profiles instead of appearing only when a package skill collides.
Bundled skills version with the CLI. Where Mars has provided them, a profile that
needs one on every turn may put its suffixed name in `skills.load` (or receive it
through `--skills`); its full body then follows the same loaded-skill path described
below and its trigger is omitted. With a Mars older than 0.20 the name is a missing
skill.
They do not belong in `skills.available`, because Mars deliberately does not project
provided skills into harness-native skill directories.

Package skills continue to use the harness-native loading path described below.

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
| `available` | The skill's name and description under `# Available Skills`, grouped by [type](https://github.com/meridian-flow/mars-agents/blob/main/docs/config/skill-compilation.md#type). The agent loads it itself through the harness's own skill mechanism, such as Claude Code's Skill tool |

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

**Package bundle interface.** Package skill content comes from the Mars
launch-bundle JSON, not from Meridian reading `.mars/skills/` on disk. Mars owns
the `.mars/` directory schema, skill variant selection, and compilation:

```
Mars launch-bundle → skills.loaded[{name, skill_type, source_path, body}]
                   → skills.available[{name, skill_type, description, source_path}]
                   → skills.missing[name]
```

Meridian receives already-resolved content and:
- Renders type-group headers (`## Principle`, `## Guardrail`, etc.) for loaded skills
- Lists available skills by name and description, grouped by type, with a load nudge
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

A profile has four separate permission fields:

- `tools` says which tool calls the model may make. `allow` runs a matching call without
  asking, `deny` blocks it, and `ask` requires human approval where the harness supports it.
- `sandbox` limits what a call that does run may touch on disk.
- `network` decides whether shell commands may connect to the public internet, host
  services, or local sockets. `allowlist` permits only `network-allow` hosts, less any
  `network-deny` hosts; `allow` can also carry denies. An omitted value follows the sandbox:
  constrained modes deny, `danger-full-access` allows, and an unset/default sandbox stays
  harness-native. It does not decide built-in web tools; use `tools: web` for those.
- `approval` supplies the harness's fallback prompt behavior for calls that `tools` does not
  decide. It does not widen an allowlist or turn an explicit `ask` into `allow`.

These fields are not substitutes for one another. `edit: deny` removes edit tools, but an
allowed shell can still write with redirection. Use a sandbox for filesystem confinement;
where the table says the sandbox is partial or not enforced, also restrict every tool that
can write.

```yaml
tools:
  'bash(rg *)': allow     # scoped key: only matching shell commands
  'bash(git push *)': ask
  edit: deny
sandbox: read-only
network: deny
approval: never
```

An allowlist can name exact hosts and IP addresses, `*.x` for subdomains only, and
`**.x` for the apex plus subdomains:

```yaml
network: allowlist
network-allow: [github.com, "*.github.com", "**.pypi.org", 127.0.0.1]
network-deny: [uploads.example]
```

Mars lowercases, sorts, and deduplicates these sets. Plain lists add across user, project,
local, profile, overlay, and launch CLI layers; a `{replace: [...]}` value clears lower
entries where that surface permits replacement ([how lists combine](https://github.com/meridian-flow/mars-agents/blob/main/docs/config/mars-toml.md#adding-to-or-replacing-a-permission-list)). Denies win. An empty allowlist becomes
`deny`. See Mars's [network profile reference](https://github.com/meridian-flow/mars-agents/blob/main/docs/config/agent-profiles.md#network)
for the complete host grammar, precedence, and replacement rules.

A *spawn* is a headless agent launched with `meridian spawn`; a *primary* is the
interactive session started with `meridian`. Supported profile rules bind both roles.
Cursor is spawn-only. A profile limits the model, not the human operating a primary;
for example, an OpenCode user can intentionally switch to another agent or choose
"always allow" in the TUI.

The table uses **Native** when the harness carries the requested meaning, **Partial** when
only some of it reaches the harness, and **Not enforced** when it has no carrier. A specific
rule can still be unsupported because Mars has no native name for that capability. Run
`meridian spawn -a NAME --dry-run` before relying on a profile: its `Permissions:` section
is the per-launch report, including each gap and the native flag or setting used. The JSON
form exposes the same lines as `permission_report`.

| Harness | `tools` | `sandbox` | `network` | `approval` | Meridian platform rules |
|---|---|---|---|---|---|
| **Claude** | **Native**, with caveats. Plain and scoped `allow`/`deny`/`ask` use native permissions; a whole-policy allowlist also limits `--tools`. Claude checks file paths only through `Edit(path)` and `Read(path)`, so scoped `write`/`edit`/`notebook` rules become `Edit(path)` and scoped `read`/`glob`/`grep` rules become `Read(path)`: a scoped deny also refuses Claude's other file edits (or reads) at that path, and `--tools` still lists the tool itself (`Write`, `Grep`). **Partial** for scoped-only allowlists because Claude's `manual` mode still auto-approves Read/Grep/Glob and Bash calls it classifies as read-only, and for a spawn's plain `bash: deny` under `approval: default` or `confirm`, which that mode carries so the platform grants can run. Only a whole-policy `*: deny` allowlist uses `dontAsk`. | **Partial** for `read-only` and `workspace-write`: the native sandbox confines Bash subprocesses, not Edit/Write. **Native** for `danger-full-access`, which disables it. A spawn-capable profile's standalone `meridian spawn ...` command is excluded from the sandbox and runs fully outside it. | In an enabled sandbox, **Native** for external host filtering with `strictAllowlist`, `allowedDomains`, and `deniedDomains`; denies win. `**.x` becomes Claude's equivalent pair `x` plus `*.x`. A finite allowlist explicitly disables local binding and Unix sockets from ordinary user settings unless it contains `localhost` or `127.0.0.1`; either entry enables Claude's coarse switches for all local binding and all Unix sockets, so that case is **Partial**. On Linux, host localhost is also **Partial**: it is reachable only through Claude's proxy, and clients honoring `NO_PROXY` bypass that proxy (`curl --noproxy '' ...` uses it). Strict deny is **Partial** on Linux because processes inside the same sandbox can still connect over its private TCP loopback. In the user settings root selected by `CLAUDE_CONFIG_DIR`, a non-empty `sandbox.network.allowedDomains` or either proxy-port key produces an attributed warning because the setting can widen access beyond Meridian's projection. The warning does not change the permission report or child guardrail, which compare only Meridian's projected policy. `deniedDomains` is narrowing and is not modelled; project `.claude/settings*.json` did not widen the launch-local strict allowlist. An excluded `meridian spawn ...` command runs outside the sandbox and can connect. Under `danger-full-access`, restrictions are **Not enforced**. | **Native** without an allowlist or `ask`: `auto` uses `acceptEdits`, `confirm` uses `manual`, and `never` bypass prompts. **Partial** with an allowlist or `ask`, which stays authoritative instead of being bypassed. | **Native** sub-agent and built-in-agent denies. **Partial** when built-in agents are enabled, because Claude cannot deny generic `Agent` while retaining them. Exact and prefix Meridian-command grants are native unless a stricter profile rule blocks them; adding Bash to a whole allowlist also exposes Claude's auto-approved read-only Bash class. |
| **Codex** | **Partial**. Exact unscoped `bash`, `web`, `web_search`, and `image` allow/deny rules use native switches. A whole-policy allowlist turns off web search and image viewing but leaves the shell on because Codex reads files through it. Scoped rules, `ask`, per-capability allowlists, and every other capability are **not enforced**. | **Native** for every explicit non-default mode. An unset value is omitted; Codex chooses its default. Before a `read-only` or `workspace-write` launch, Meridian rewrites `$CODEX_HOME/rules/meridian.rules`; its `meridian spawn` rule runs those commands outside the sandbox for every Codex session using that home. | `workspace-write` is **Native** for `allow` without denies (`network_access=true`) and for `deny` or an empty allowlist (`false`). Host sets use Codex's network proxy and one inline domain map: finite allows are `"host"="allow"`; `allow` plus denies adds `"*"="allow"`; denies are `"host"="deny"`. Any `allow` entry under `features.network_proxy.domains` in the child-selected `$CODEX_HOME/config.toml` or `.codex/config.toml` in the child cwd or an ancestor produces an attributed warning because it can widen access beyond Meridian's projection. A boolean `features.network_proxy` declares no domains and does not warn. Warnings do not change the permission report or child guardrail, which compare only Meridian's projected policy. Domain `deny` entries are narrowing and are not modelled. Host-map cases are **Partial** because Codex always leaves local services reachable. `read-only` stays offline in Meridian's projection and reports any `allow` or non-empty allowlist **Not enforced**. Under `danger-full-access`, restrictions are **Not enforced**. The user-wide `meridian spawn` rule runs matching commands outside constrained modes with network access. | **Native** for `auto` (`on-request`) and `never` (`never`). **Partial** for `confirm`, which is also `on-request` because Codex has no stricter mode. | **Partial**. By default Meridian disables Codex's built-in multi-agent feature; `[harness.codex] allow_builtin_agents = true` removes that rule. The user-wide rules file grants the `meridian spawn` prefix under a constrained sandbox; other exact or prefix Meridian commands and the task-list deny are not projected. |
| **OpenCode** | **Native** on the selected `meridian-<profile>` config agent in V1 and V2, for spawns and primaries. Scoped rules and allowlists work. **Partial** in V2 where distinct `write` and `patch` meanings collapse onto `edit`; the stricter conflicting effect wins. | **Not enforced**. | `allow` without denies is already satisfied because shell network is unrestricted; `allowlist`, deny exceptions, and `deny` are **Not enforced**. | **Native** unless a `*` tool rule already decides unmatched calls: `confirm` asks in a primary and denies in a headless spawn; `auto` and `never` allow unmatched calls without prompting. `default` leaves OpenCode's behavior, except an otherwise unrestricted spawn keeps Meridian's historical allow-all fallback. | **Native** sub-agent denies and exact/prefix Meridian-command grants. Completion and read-only command grants remain authoritative over overlapping profile rules; a stricter rule may still restrict the spawn-launch grant. |
| **Pi** | **Native** for mapped tools. Plain rules use `--tools`/`--exclude-tools`; scoped rules, allowlists, and `ask` use Meridian's `meridian-permissions` Pi extension, which checks each simple command of a Bash call. A Bash allowlist, a Bash `deny`/`ask` for unmatched commands, or a scoped Bash `ask` also refuses shell syntax the extension cannot split (expansion, redirection, loops, here-docs). **Partial** for scoped Bash denies when Bash allows unmatched commands (`bash: allow` or `*: allow`): a command the extension cannot split runs unchecked. | **Not enforced**. | `allow` without denies is already satisfied because shell network is unrestricted; `allowlist`, deny exceptions, and `deny` are **Not enforced**. | **Not enforced**. This does not affect tool-level `ask`, which the extension enforces separately. | **Native**. Pi has no native sub-agent or built-in-agent tools, so those denies already hold; when a policy could restrict the shell, the extension grants exact and prefix Meridian commands. |
| **Cursor** | **Not enforced**, including restrictive profiles and `ask`. | **Native**: `danger-full-access` disables Cursor's binary sandbox. **Partial**: `read-only` enables it, which still allows workspace writes, so it runs as `workspace-write`; `workspace-write` disables it because Cursor has no middle setting. Unset uses Cursor's default. | `allow` without denies is already satisfied because shell network is unrestricted; `allowlist`, deny exceptions, and `deny` are **Not enforced**. | **Native** for `auto` (`--force`) and `never` (`--yolo`). `confirm` is **Not enforced**. | **Not enforced**. |

If Mars predates the bundle's `network_source` field, Meridian refuses the launch and asks
you to upgrade Meridian; it names the selected executable because Meridian's bundled Mars
wins over `PATH`. It never falls back to a harness network default.

The same harness limits apply when `approval` or `sandbox` comes from a CLI flag,
environment variable, [Mars agent overlay](configuration/mars.md#agent-runtime-overlays),
or [Meridian config](configuration.md). `network` can also come from a Mars agent overlay
or model policy. `tools` comes from the profile.

### Capability names

A `tools` rule names a capability, not a harness tool. Mars translates each capability into
the target harness's own tool names when it compiles the profile; Meridian applies what Mars
names. These are the capabilities a profile can write:

| Capability | Covers |
|---|---|
| `bash` | shell commands; scope with a command prefix, `bash(git log *)` |
| `read` | reading a file |
| `write` | creating or overwriting a file |
| `edit` | changing a file (on Claude and Pi this also covers writing) |
| `notebook` | editing a Jupyter notebook |
| `glob` | finding files by name |
| `grep` | searching file contents |
| `image` | viewing an image file |
| `web` | `web_search` and `web_fetch` together |
| `web_search` | searching the web |
| `web_fetch` | fetching a URL |
| `agent` | starting or messaging sub-agents |
| `task` | the task list (todo tools) |
| `skill` | loading a skill on demand; scope with a skill name, `skill(init)` |
| `ask_user` | asking the human a question |
| `plan_mode` | entering or leaving plan mode |
| `workflow`, `cron`, `notifications`, `worktree`, `lsp`, `monitor`, `send_user_file`, `schedule_wakeup`, `remote_trigger`, `tool_search` | Claude-specific tools of the same names (`tool_search` also exists on Codex, `lsp` on OpenCode) |
| `*` | every tool no other rule names; `'*': deny` makes the profile an allowlist |
| `mcp(server)`, `mcp(server/tool)` | MCP tools |

The harness name each capability becomes, and which harnesses have no tool for it, is in
Mars's [tool name projection table](https://github.com/meridian-flow/mars-agents/blob/main/docs/config/agent-compilation.md#tool-name-projection).
A capability a harness lacks shows in `--dry-run`: a deny of a tool it does not have is
already met, and a rule Mars cannot name there is reported `unsupported`.

### Allowlists and scoped rules

- A map containing `'*': deny` is a whole-policy allowlist: only the tools it allows
  exist. The list form `tools: [read, grep]` was removed in schema v1; `meridian mars migrate`
  rewrites it to `read: allow` and `grep: allow`, which restricts nothing.
- A capability with scoped allows and no unscoped rule is an allowlist for that
  capability. With only `'bash(rg *)': allow`, unmatched Bash calls are denied. Add
  `bash: allow` to allow the rest. An unscoped `*` rule also decides unmatched calls
  and ends the scoped-only allowlist.
- Narrower capabilities win where they overlap. `edit` covers writes too, but
  `edit: allow` with `'write(src/*)': allow` still permits writes only under `src/`, just
  as `edit: deny` with `write: allow` leaves writes allowed.
- `approval: never` means "do not prompt"; it does not mean "ignore `tools`."
- On a Claude spawn under `approval: default` or `confirm`, a plain `bash: deny` is not
  emitted as `--disallowedTools Bash`, which would also block the spawn's
  `meridian spawn done`/`rearm` grants. The permission mode (`manual`) refuses the other
  Bash calls instead, but Claude still runs Bash calls it classifies as read-only, such as
  `cat` or `ls`; the dry-run marks the rule `partial`. Under `auto` or `never` the
  deny is native and those grants are blocked (also reported).
- Mars normalizes a Bash prefix ending in `:*` to a space before the wildcard:
  `bash(git push:*)` becomes `bash(git push *)`. Write the space form in new profiles.
  Colons in non-Bash patterns remain literal.
- MCP rules use `mcp(server)`, `mcp(server/tool)`, `mcp(server/*)`, `mcp(*/tool)`, or
  `mcp(*/*)`. `*` is the only wildcard, names are case-sensitive, and bare `mcp` is
  invalid. Mars supplies each harness's native spelling; do not put `mcp__...` or
  `Mcp(...)` wire names in a Mars-native profile.
- MCP rules grant or deny tools; they do not register servers. Package
  `mcp/<name>/mcp.toml` definitions are installed into harness config by
  `meridian mars sync`. The launch bundle does not carry those server definitions.

### Your own settings stay in force

Meridian does not strip a harness's own configuration to make a profile stricter.
Claude reads your user, project and local settings files as it always does, so their
`permissions.allow` entries run even under an allowlist; `--dry-run` lists them on a
`[partial] source:` line. Claude user network settings and Codex user or ancestor-project
proxy-domain allows can also widen access beyond Meridian's projection. Dry runs and
launches name those files in warnings; the permission report and child-spawn guardrail
still compare only Meridian's projected policy. Under OpenCode, a headless spawn with `approval: default` and
no rule naming an OpenCode tool gets an allow-all fallback; once the profile names a
tool, OpenCode's defaults and your OpenCode config decide the rest.

### `ask` and headless spawns

A primary can put a human in the loop: Claude, OpenCode, and Pi present a native prompt
for a mapped `ask` rule. Codex and Cursor do not enforce tool-level `ask`.

A spawn has no human to answer, so a supported `ask` is refused immediately and never
routed to the parent agent. Claude disables permission prompts, OpenCode lowers the rule
to native `deny`, and Pi's extension blocks it with Meridian's headless reason. Refusals
that the harness reports are recorded in `permission_requests.jsonl` and shown by
`meridian spawn show`. OpenCode supplies its own denial text, and a tool removed from its
catalog produces no attempted-call event to journal.

Runtime approval requests caused by Codex or OpenCode's approval mode are also refused
and journaled in spawns. In a managed Codex primary they are recorded and remain pending
for a controller; the Codex TUI itself cannot answer them. Do not rely on Cursor approval
prompts; that path is unobserved.

### Platform rules

Meridian adds launch rules independently of the profile so delegation stays observable:

- Native sub-agent tools (`agent`) are denied on every launch unless the project's Mars
  config makes native agent copies the delegation mechanism for that harness. No profile
  rule opts out of this deny.
- Spawns also deny task-list tools (`task`) unless the profile has an explicit unscoped
  `task: allow` or `task: ask`. A `'*': allow` does not count.
- A platform deny overrides the profile's rules for its capability, scoped allows such as
  `agent(coder): allow` included.
- Claude/Codex built-in agents are denied unless the corresponding
  `[harness.<id>] allow_builtin_agents = true` setting applies.

Meridian also adds the permissions its control plane needs; profile authors do not declare
them:

- **State roots.** A sandboxed launch can reach its task checkout, the active work and KB
  roots, the project's runtime state, and the bounded temporary directory used for control
  sockets. The launcher pre-creates this project's lifetime lock, which child commands open
  read-only; the shared `.locks` directory is not writable from the sandbox. Codex's
  `read-only` sandbox can only read the projected roots. Claude `read-only` and every
  `workspace-write` launch can write Meridian runtime state, so `spawn message` and `cancel`
  do not claim a separate delegation boundary.
- **Meridian commands.** Meridian adds completion and read-command grants to every launch;
  a profile that can spawn also gets the `meridian spawn ...` launch commands. Completion
  (`spawn done` / `rearm`) is control-plane liveness and a profile cannot restrict it.
  Read and launch commands remain profile-restrictable: a narrower or equal scoped
  profile `deny`/`ask` stays stricter. Each grant is exact or prefix-matched in the target
  harness's syntax; a harness that cannot honor the precedence reports it as partial. Run
  `meridian spawn -a NAME --dry-run` to see the exact command list and which grants that
  harness can enforce.
- **Child-spawn escape.** Claude excludes a spawn-capable profile's standalone
  `meridian spawn ...` calls from `read-only` and `workspace-write` Bash sandboxes, and
  only those explicit profile modes. If the profile leaves `sandbox` unset while Claude's
  own settings enable it, nested creation stays inside that sandbox and the PID-namespace
  guard refuses it; set `sandbox:` in the profile. Codex maintains
  `$CODEX_HOME/rules/meridian.rules` before spawn-capable launches in those modes, allowing
  the same standalone prefix outside Codex's sandbox. The whole matching command runs
  outside the sandbox, including its filesystem and network access. Codex's rule is
  user-wide: it applies to every Codex session using that home, including profiles without
  spawn capability and sessions not launched by Meridian. Meridian owns and rewrites only
  `meridian.rules`; it does not touch other Codex rules files.
- **Child-spawn CLI guardrail.** Every Meridian-launched harness receives its resolved
  sandbox, spawn capability, and Meridian-projected network in a launcher-owned boundary
  record. Only `read-only` and
  `workspace-write` callers have a floor, and both sides are compared by what their harness
  actually enforces. OpenCode and Pi enforce neither sandbox nor network, so they count as
  `danger-full-access` and online; Cursor's binary sandbox counts `read-only` as
  `workspace-write` and `workspace-write` as `danger-full-access`, with open network. A
  caller that counts as `danger-full-access` has no floor, and a child that a sandboxed caller
  is refused because of its harness gets a refusal naming that harness. Pass `--harness claude` or `--harness codex` (or a model that
  routes there) to keep such a child. `spawn`, `spawn --continue`, and `spawn --fork` (including `--dry-run`)
  refuse a floored caller whose profile cannot spawn, a child whose resolved sandbox is
  weaker, or a child whose projected network is not contained by the caller's. An offline
  child is contained by every caller; otherwise child allows must stay within caller allows
  and child denies must preserve caller denies. Each harness owns that pure projection,
  which also drives its `network:` report line. User harness config can widen actual
  access and is reported separately as a warning; it does not alter the floor. Harness-native passthrough
  arguments after `--` are refused
  because they can override Meridian's resolved sandbox or sandbox flags. A child emits its
  own resolved floor to its descendants.
  A missing network key creates no floor for compatibility with an older running caller. A
  network refusal says to narrow the child's allowed hosts, preserve the caller's denied
  hosts, or widen the caller policy.
  `danger-full-access` and `default` callers, top-level commands, and shells that were not
  launched by Meridian have no floor; read commands, `spawn done`, and `spawn rearm` do
  not consult it. A non-empty `subagents:` list supplies spawn capability but does not yet
  restrict which agent name the caller selects; declared-child enforcement is tracked in
  [#742](https://github.com/haowjy/meridian-cli/issues/742).
- **Loud failure for compound shell calls.** On Linux, Meridian records the harness
  launcher's PID namespace. A child launch attempted from a different namespace stops with
  an error rather than starting a child that dies with the sandboxed shell. Run
  `meridian spawn` as a standalone shell call, without `cd ... &&`, pipes, command
  substitution, or a leading environment assignment. The check is skipped where procfs does
  not expose PID namespaces, including macOS.

The floor is a guardrail against CLI escalation, not confinement against an agent that
writes files. A sandbox plus spawn capability does **not** confine an adversarial agent:
the escaped process reads runtime requests, session state, project hooks, workspace/context
configuration, shell startup files, and executables that some sandboxed callers can modify.
Codex's rule is also user-wide, so a Codex session not launched by Meridian has no floor
even though the rule may let its standalone `meridian spawn ...` command escape. Treat
those inputs as trusted. Real confinement work is tracked in
[#747](https://github.com/haowjy/meridian-cli/issues/747).

The dry-run report marks platform decisions as `platform` and calls out partial or missing
projections. Mars supplies each harness's native agent/task names in the launch bundle;
Meridian does not keep a second name table. A launch snapshot written before that metadata
existed takes the names from a fresh bundle for the same harness when it replays; only if Mars
cannot build one are those platform denies reported as unsupported.

OpenCode receives all Meridian-owned launch settings in `OPENCODE_CONFIG_CONTENT`, including
the reserved config agent. Meridian no longer sets `OPENCODE_PERMISSION`.

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
