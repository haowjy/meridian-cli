# Mars configuration and agent runtime

Use Mars config for package materialization, project routing defaults, model
aliases, and per-agent runtime policy. The generated `.mars/agents/` profiles
are build output; do not edit them directly.

## User packages and bare directories

Packages you install for yourself live at the Mars user level, `~/.mars/`: its `mars.toml`,
`config.toml` and store are all in that one folder (`MARS_HOME` moves it; see the
[Mars user-level guide](https://github.com/haowjy/mars-agents/blob/main/docs/cli/user-level.md)).
Add them with `--user`:

```bash
meridian mars --user add haowjy/meridian-base
meridian mars --user sync
```

`meridian mars --user …` forwards `mars --user …`; it needs Mars 0.41.0 or newer, and
Meridian says so before forwarding when the Mars it runs is older.

Mars chooses the root, not Meridian. Meridian runs Mars in your working directory, so a
relative path you type resolves from there. When Meridian's project (`-C` or
`MERIDIAN_PROJECT_DIR`) does not contain the working directory, for example an agent in a
worktree, Meridian passes it as `--root`, which only starts Mars's walk-up; a `--root` or
`--user` you type is forwarded instead. Lookups (launches, `mars agents`, `mars skills`, `mars models`)
use the nearest project `mars.toml`, and the user level when there is none. The user level has
no `targets`, so it permits every harness. Your home directory's `mars.toml` is never a project,
and a Meridian project that is exactly your home directory is not passed as `--root`.

Inside a project, user-level items are installed by the project's own `meridian mars sync`,
beside its packages and kept out of its commits; an item of the same name from the project's
packages wins. Model aliases and routing config come from the project's Mars config.
`meridian config show`, `meridian context` and `meridian doctor` report the root Mars chose as
`mars` (`kind` and `path`; the user level's store and `config.toml` are in its `path`).

Writes without `--user` need a project. `meridian mars add` and `link` would create one where
there is none, so Meridian refuses them when Mars reports `project_not_found` there; every
other write Mars refuses itself, with a message naming `--user`. `meridian mars init` is the explicit way to start a project: it writes
only `mars.toml`; the next Meridian runtime write mints the committed `[project] id` in
`meridian.toml`.

Every `meridian mars` command that syncs (`sync`, `add`, `remove`, `upgrade`, `repair`,
`rename`, `override`, `adopt`, `link`), and `meridian init`'s `add` and `link`, passes
Meridian's own bundle to the sync as `--provided-root meridian@<version>=<dir>`, so targets no
harness Meridian launches (`.agy`) get Meridian's skills; Mars keeps that layer on later syncs.
`meridian mars --user` passes it to the same commands. The user level installs nothing from
it; with a Mars that counts a host's skills for every agent (later than 0.41.0), a user-level
sync stops reporting a user-level agent that names a Meridian skill such as `work__meridian`
as missing it.
Launches read the bundle in place.

Mars runs in your working directory and takes the nearest `mars.toml` above it. Inside a
project with a nested `mars.toml` (a package checkout under it), that nested one is the Mars
root, while Meridian's state and work items stay with the outer project.

### `~/.meridian/mars`

Before the Mars user level, Meridian kept user packages in its own Mars project at
`~/.meridian/mars`. Meridian never deletes or writes it. For one minor release, while the folder
exists, lookups still read it (as Mars's deprecated `--fallback-root`), each command warns once,
and `meridian doctor` reports it as `legacy_user_mars_root`. The next minor release stops
reading it. To move it, and any older Mars layout, into the user level, run the
[migration script](../upgrading.md#mars-chooses-the-root-meridianmars-retires): a dry run on its
own, `--apply` to move.

## What a package installs

A Mars package can install agents, skills, hooks, and MCP servers. `meridian mars sync` renders a package's hooks into each enabled harness's own config (for example `.claude/settings.local.json`, `.codex/hooks.json`, `.opencode/plugins/`, `.pi/extensions/`). These are harness hooks, separate from Meridian's event [hooks](../hooks.md). The package author declares them in `hooks/<name>/hook.toml`; see the [Mars reference](https://github.com/haowjy/mars-agents/blob/main/docs/config/mcp-and-hooks.md#declaring-hooks-in-a-package).

## Project routing defaults

```toml
[settings]
targets = [".claude", ".codex", ".opencode", ".pi"]
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
`.claude`, from `settings.targets`). Mars then materializes only qualifying agents for that harness;
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
2. **Meridian stays the route** — `meridian spawn subagents` lists it without the
   native marker, so callers launch it with `meridian spawn -a <name>`; the native copy
   (`Agent({subagent_type: "<name>"})` for Claude) remains the escalation route.

The former `agent_copy` fan-out allowlist key is removed. If present in
`mars.toml`, mars emits a migration warning pointing to
`[settings.meridian.fanout].agents` and ignores the old value.

For Claude launches, Meridian uses the same boundary for native `Agent()`
delegation. Generic Claude `Agent` is allowed only when the last sync actually
installed at least one agent into Claude's native directory, as `mars agents list`
reports (`native-harnesses`). That covers `agent_copy` in any Mars config layer
(including the user `config.toml`), `agent_emission = "always"`, and a bare
`mars sync` run outside Meridian. Otherwise the launch denies native `Agent` and
guides delegation back to `meridian spawn`. Claude built-in agents (`Explore`,
`Plan`, `General-purpose` / `general-purpose`) stay denied.

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
To completely avoid Claude-side delegation, also make sure no sync installs Claude-native
agents: no Claude `agent_copy`, no `agent_emission = "always"`, and sync through
`meridian mars sync`:

```toml
[settings.meridian.agent_copy]
harnesses = []
```

## Model aliases

Project-local aliases live under `[models.<alias>]`:

```toml
[models.fast]
harness = "claude"
model = "claude-haiku-5-5"
provider = "anthropic"
description = "Small, fast model for quick edits."
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
`[agents.<name>.tools]` table merges into the profile's tool policy. The rule form is a map
of tool key to `allow`, `deny` or `ask`; an entry replaces the same key in the profile, in the
project-wide `[settings.tools]` rules and in the package's `[package.tools]`, for that agent
only. Rules with different keys follow the strictest-wins precedence
([details](../agent-profiles.md#allowlists-and-scoped-rules)):

```toml
[agents.coder.tools]
"bash(git push *)" = "ask"
"bash(pkill -f *)" = "allow"   # exempts coder from a project-wide deny of this key
```

The `allowed` and `disallowed` lists still work but are deprecated: write the map form. The
lists add rules to the profile's; `{ replace = [...] }` clears the profile's lower rules of
that action, never a project-wide rule or a map entry. A table may mix map entries and lists.
Two spellings of one key in one table (`Bash` beside `bash`) fail the load, and so does a
string-valued key that is not a tool name.
The merged rules reach both native agent files and the launch bundle, so Meridian launches
with them. A list-valued `tools` key under `[agents.<name>]` is a parse error.

Project-wide `deny`, `ask` and `allow` rules live in `[settings.tools]`, and a package's own in
`[package.tools]`; see
[Project-wide tool rules](../agent-profiles.md#project-wide-tool-rules).

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

Effort follows this order inside Mars, not in Meridian: Meridian sends `--effort` and
`MERIDIAN_EFFORT` as `--effort`, and `primary.effort` from `meridian.toml` as
`--config-effort`, which sits above the alias's `default_effort`. Mars then moves the
result to a level the model and harness take. Meridian prints the move only when the level
came from `--effort` or `MERIDIAN_EFFORT`
([Effort by harness](../agent-profiles.md#effort-by-harness)).

Local Mars overrides win over project Mars config for overlay values, and the Mars user
`config.toml` sits below both: `config.toml < mars.toml < mars.local.toml < CLI`
(file locations: [User `config.toml`](../upgrading.md#user-configtoml-now-applies-mars-0233)).

### Observability

- `meridian mars export --json` shows the compiled package/materialization plan.
- `meridian mars models aliases --json` shows static aliases;
  `meridian mars models catalog --json` shows raw model metadata.
  `meridian mars models list` is a curated human view, and
  `resolve ALIAS` resolves an individual alias.
- `meridian spawn --dry-run` reflects overlay-aware routing and provenance
