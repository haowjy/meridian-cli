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
| `tools` | map or list[str] | — | Per-tool rules: `bash: allow`, `edit: deny`, or `web: ask`. A list is an allowlist containing exactly those names. A scoped key such as `bash(rg *)` limits a rule to matching calls. MCP rules use `mcp(server)` or `mcp(server/tool)`. What each harness enforces: [Permissions by harness](#permissions-by-harness) |
| `disallowed-tools` | list[str] | `[]` | Tools to deny, with the same names and scoped keys as `tools`. Equivalent to `deny` entries in the `tools` map |
| `sandbox` | str | — | `default`, `read-only`, `workspace-write`, or `danger-full-access`. Harness support differs: [Permissions by harness](#permissions-by-harness) |
| `effort` | str | — | Effort level (`low`, `medium`, `high`) |
| `approval` | str | — | When the harness stops to ask: `default`, `confirm`, `auto`, or `never`. `yolo` is a deprecated spelling of `never`. See [Permissions by harness](#permissions-by-harness) |
| `autocompact` | int | — | Context-token compaction threshold (minimum 1000); harness support differs. See [Compaction by harness](#compaction-by-harness) |
| `autocompact_pct` | int | — | Context-window percentage that triggers compaction (1–100); harness support differs. See [Compaction by harness](#compaction-by-harness) |
| `resident-rearm-budget` | int | — | Maximum resident deadline extensions; omitted means unlimited |
| `harness-overrides` | map | `{}` | Harness-native launch config selected for the resolved harness; see [Harness-native passthrough](#harness-native-passthrough) |
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

`harness`, `sandbox`, `approval`, `effort`, `autocompact`, `autocompact_pct`

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
    override: { autocompact: 200000 }
```

Fallback only activates when:

- the head candidate's harness is unavailable, **and**
- the user did not explicitly set a model with `-m` / `MERIDIAN_MODEL`

Legacy `fallback-order` and `fanout` are rejected. Order `model-policies`
rules directly and add `no-fallback: true` for rules that should never be
considered for fallback.

## Compaction by harness

`autocompact` is an absolute token value; `autocompact_pct` is a percentage. They
are separate controls, not alternate spellings of the same number.

| Harness | `autocompact` | `autocompact_pct` |
|---|---|---|
| **Claude** | `--autocompact <tokens>` for values from 100000 through 1000000 | `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=<percent>` |
| **Codex** | `-c model_auto_compact_token_limit=<tokens>` | `-c model_post_turn_compact_threshold_percent=<percent>` |
| **OpenCode**, **Pi**, **Cursor** | Not applied | Not applied |

Claude applies the percentage to its autocompact window, so both fields can be
set together. A Claude token value outside its native 100000–1000000 range is
not passed to the CLI. `--dry-run` reports every applied value and every
unsupported value or harness.

Earlier Meridian builds wrote `autocompact`'s token count into Claude's
percentage environment variable and did not apply `autocompact_pct`. Existing
Claude profiles using `autocompact` now set the token window instead; a value
such as `200000` previously exceeded the effective percentage range and left
Claude at its default, but now selects a 200000-token autocompact window.

## Harness-native passthrough

Mars selects `harness-overrides.<resolved-harness>` and passes the block to Meridian
without interpreting it. The selected block is applied as written and can change anything
in Codex's config, including providers and MCP server commands. Installing a package that
carries harness overrides is therefore installing code you trust. Blocks in already-installed
packages that older Meridian builds left inert now take effect. Codex flattens nested mappings
to dotted config keys and emits TOML-compatible scalar, array, and inline-table values as
`-c` overrides. For example, this enables network access inside Codex's `workspace-write`
sandbox:

```yaml
harness-overrides:
  codex:
    sandbox_workspace_write:
      network_access: true
```

Meridian-owned Codex settings win: native entries that collide with its model, effort,
sandbox, approval, writable-root, tool-switch, agent, feature, or notification config are
skipped. Meridian does not synthesize Codex MCP config, so `mcp_servers.*` entries pass
through like other native config. A key segment containing `.`, `=`, whitespace, or quotes
cannot be addressed by Codex's `-c` syntax and is reported as unsupported. `--dry-run`
shows each applied or skipped key, for example
`[native] native_config: sandbox_workspace_write.network_access = true -> -c sandbox_workspace_write.network_access=true`.
Claude, OpenCode, Pi, and Cursor do not apply native config; their report lists the key
names as `[unsupported] native_config: ... -> not applied for <Harness>`.

The selected harness block is the config tree. A key named `native-config` inside it is a
literal root key and is flattened like any other key; Meridian does not unwrap it.

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

A profile has three separate permission fields:

- `tools` says which tool calls the model may make. `allow` runs a matching call without
  asking, `deny` blocks it, and `ask` requires human approval where the harness supports it.
- `sandbox` limits what a call that does run may touch on disk.
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
approval: never
```

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

| Harness | `tools` | `sandbox` | `approval` | Meridian platform rules |
|---|---|---|---|---|
| **Claude** | **Native**, with caveats. Plain and scoped `allow`/`deny`/`ask` use native permissions; a whole-policy allowlist also limits `--tools`. Claude checks file paths only through `Edit(path)` and `Read(path)`, so scoped `write`/`edit`/`notebook` rules become `Edit(path)` and scoped `read`/`glob`/`grep` rules become `Read(path)`: a scoped deny also refuses Claude's other file edits (or reads) at that path, and `--tools` still lists the tool itself (`Write`, `Grep`). **Partial** for scoped-only allowlists because Claude's `manual` mode still auto-approves Read/Grep/Glob and Bash calls it classifies as read-only, and for a spawn's plain `bash: deny` under `approval: default` or `confirm`, which that mode carries so the platform grants can run. Only a whole-policy `*: deny` allowlist uses `dontAsk`. | **Partial** for `read-only` and `workspace-write`: the native sandbox confines Bash subprocesses, not Edit/Write. **Native** for `danger-full-access`, which disables it. A spawn-capable profile's standalone `meridian spawn ...` command is excluded from the sandbox and runs fully outside it. | **Native** without an allowlist or `ask`: `auto` uses `acceptEdits`, `confirm` uses `manual`, and `never`/`yolo` bypass prompts. **Partial** with an allowlist or `ask`, which stays authoritative instead of being bypassed. | **Native** sub-agent and built-in-agent denies. **Partial** when built-in agents are enabled, because Claude cannot deny generic `Agent` while retaining them. Exact and prefix Meridian-command grants are native unless a stricter profile rule blocks them; adding Bash to a whole allowlist also exposes Claude's auto-approved read-only Bash class. |
| **Codex** | **Partial**. Exact unscoped `bash`, `web`, `web_search`, and `image` allow/deny rules use native switches. A whole-policy allowlist turns off web search and image viewing but leaves the shell on because Codex reads files through it. Scoped rules, `ask`, per-capability allowlists, and every other capability are **not enforced**. | **Native** for every explicit non-default mode. An unset value is omitted; Codex chooses its default. Before a `read-only` or `workspace-write` launch, Meridian rewrites `$CODEX_HOME/rules/meridian.rules`; its `meridian spawn` rule runs those commands outside the sandbox for every Codex session using that home. | **Native** for `auto` (`on-request`) and `never`/`yolo` (`never`). **Partial** for `confirm`, which is also `on-request` because Codex has no stricter mode. | **Partial**. By default Meridian disables Codex's built-in multi-agent feature; `[harness.codex] allow_builtin_agents = true` removes that rule. The user-wide rules file grants the `meridian spawn` prefix under a constrained sandbox; other exact or prefix Meridian commands and the task-list deny are not projected. |
| **OpenCode** | **Native** on the selected `meridian-<profile>` config agent in V1 and V2, for spawns and primaries. Scoped rules and allowlists work. **Partial** in V2 where distinct `write` and `patch` meanings collapse onto `edit`; the stricter conflicting effect wins. | **Not enforced**. | **Native** unless a `*` tool rule already decides unmatched calls: `confirm` asks in a primary and denies in a headless spawn; `auto` and `never`/`yolo` allow unmatched calls without prompting. `default` leaves OpenCode's behavior, except an otherwise unrestricted spawn keeps Meridian's historical allow-all fallback. | **Native** sub-agent denies and exact/prefix Meridian-command grants. A profile rule that is stricter on an overlapping command remains authoritative, so that grant is partial. |
| **Pi** | **Native** for mapped tools. Plain rules use `--tools`/`--exclude-tools`; scoped rules, allowlists, and `ask` use Meridian's `meridian-permissions` Pi extension, which checks each simple command of a Bash call. A Bash allowlist, a Bash `deny`/`ask` for unmatched commands, or a scoped Bash `ask` also refuses shell syntax the extension cannot split (expansion, redirection, loops, here-docs). **Partial** for scoped Bash denies when Bash allows unmatched commands (`bash: allow` or `*: allow`): a command the extension cannot split runs unchecked. | **Not enforced**. | **Not enforced**. This does not affect tool-level `ask`, which the extension enforces separately. | **Native**. Pi has no native sub-agent or built-in-agent tools, so those denies already hold; when a policy could restrict the shell, the extension grants exact and prefix Meridian commands. |
| **Cursor** | **Not enforced**, including restrictive profiles and `ask`. | **Native**: `read-only` enables Cursor's binary sandbox and `danger-full-access` disables it. **Partial**: `workspace-write` also disables it because Cursor has no middle setting. Unset uses Cursor's default. | **Native** for `auto` (`--force`) and `never`/`yolo` (`--yolo`). `confirm` is **not enforced**. | **Not enforced**. |

The same harness limits apply when `approval` or `sandbox` comes from a CLI flag,
environment variable, [Mars agent overlay](configuration/mars.md#agent-runtime-overlays),
or [Meridian config](configuration.md). `tools` comes from the profile.

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
  exist. The list form `tools: [read, grep]` is deprecated and only pre-approves.
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
  `cat` or `ls`; the dry-run marks the rule `partial`. Under `auto`, `never`, or `yolo` the
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
`[partial] source:` line. Under OpenCode, a headless spawn with `approval: default` and
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
  `read-only` sandbox can only read the projected roots.
- **Meridian commands.** Completion and read-only commands are always granted. A profile
  that can spawn also gets the `meridian spawn ...` launch commands. Each grant is either
  exact or prefix-matched in the target harness's syntax. Run
  `meridian spawn -a NAME --dry-run` to see the exact command list and which grants that
  harness can enforce.
- **Child-spawn escape.** Claude excludes a spawn-capable profile's standalone
  `meridian spawn ...` calls from `read-only` and `workspace-write` Bash sandboxes. Codex
  maintains `$CODEX_HOME/rules/meridian.rules` before launches in those modes, allowing
  the same standalone prefix outside Codex's sandbox. The whole matching command runs
  outside the sandbox, including its filesystem and network access. Codex's rule is
  user-wide: it applies to every Codex session using that home, including profiles without
  spawn capability and sessions not launched by Meridian. Meridian owns and rewrites only
  `meridian.rules`; it does not touch other Codex rules files.
- **Loud failure for compound shell calls.** On Linux, Meridian records the harness
  launcher's PID namespace. A child launch attempted from a different namespace stops with
  an error rather than starting a child that dies with the sandboxed shell. Run
  `meridian spawn` as a standalone shell call, without `cd ... &&`, pipes, command
  substitution, or a leading environment assignment. The check is skipped where procfs does
  not expose PID namespaces, including macOS.

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
