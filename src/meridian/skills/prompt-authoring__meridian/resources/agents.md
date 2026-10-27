# Agent Profiles

A profile is a markdown file: YAML frontmatter, then the body, which becomes the agent's
system prompt. Mars's [agent profile reference](https://github.com/meridian-flow/mars-agents/blob/main/docs/config/agent-profiles.md)
has every field; this covers what the schema doesn't show.

## Where It Lives

`meridian mars sync` compiles the source into `.mars/agents/NAME.md` with every field
intact, and Meridian always launches from that copy. Harness-native agent files exist
only where `[settings.meridian.agent_copy]` asks for them, so don't expect a harness's
own agent picker to list the agent; launch it with `meridian -a NAME` or
`meridian spawn -a NAME`.

To change a few fields of an installed package agent, write an overlay instead of
copying the file: an `[agents.NAME]` table in `mars.toml` (or `mars.local.toml` for this
machine only) sets fields such as `model`, `sandbox`, `tools`, or `description`, and the
agent keeps receiving upstream body changes. A same-named file in `.mars-src/agents/`
replaces the package's agent outright, and sync warns that it shadows it. When two
packages ship one name, Mars installs both as `NAME__SOURCE`. `meridian mars agents show
NAME` prints the installed file without overlays; the dry run shows the launch.

## What a Caller Sees

A caller routes from an inventory in its own prompt, one line per agent it may spawn:

```
- `meridian spawn -a reviewer`: DESCRIPTION | Model: DEFAULT | Declared backups: ALIASES
```

The description is the only text a caller routes on: say what work the agent does and
when to pick it over its neighbors, and name the skills a caller can attach for focus
("Attach skills for focus: ..."). The model already sits beside it, so an agent whose
name or description advertises its model gets picked for the model; describe the work.

A caller's inventory lists only the agents in its own `subagents:`.
`model-invocable: false` keeps an agent out of every inventory while `-a NAME` still
launches it.

## What Meridian Adds

Every launch gets Meridian's capabilities block (the work item, the source checkout
versus the work directory, past sessions, the knowledge base and other context roots)
and trigger lines for Meridian's own skills, read with `meridian skills NAME`.
A spawn is told its final message is the report its caller reads, with a default list of
what that holds; a primary is told it is talking to the user.

Two fields change that guidance:

- A non-empty `subagents:` makes the agent spawn-capable: it gets the inventory,
  briefing guidance, the rules for launching and collecting spawns, and permission to
  run `meridian spawn`. The list scopes the inventory; it does not yet stop the agent
  from launching an unlisted agent by name.
- `mode: primary` marks a lead. The triggers for Meridian's lead-only skills, such as
  handoff and notification, and the `meridian notify` capability reach only
  primary-mode profiles, also when one runs as a spawn. Leave workers on the default
  `subagent`, so they report to their caller instead.

Bodies and skills don't restate any of this. They say what to delegate, what to pass,
and what the report holds when it differs from the default; Meridian supplies the how,
and a copy in a prompt goes stale.

## What Reaches a Spawn

A spawn starts with fresh context: its system prompt (body, loaded skills, Meridian's
guidance), the brief, files the caller passes with `-f`, and anything carried with
`--from`. A carried spawn contributes its report; a carried session contributes pointers
to its transcript. The spawn can read the work directory, the knowledge base, the source
checkout, and past sessions. Nothing else from the caller's conversation reaches it, so
the body can't assume the caller's context.

A skill the profile loads is in the prompt in full under `# Skill: /NAME`, even when the
harness's skill list omits it (`model-invocable: false`), so the body can tell the agent
to follow `/NAME`. Under the experimental `experimental.skill_delivery = "named"` setting,
Meridian instead gives one line per loaded skill naming `/NAME` and the path to its
`SKILL.md`, under the same type headers.

## Permissions

`tools`, `sandbox`, and `network` are the policy; a sentence in the body only asks.

- `edit: deny` removes edit tools, but an allowed shell can still write through
  redirection. `sandbox: read-only` stops shell writes where the harness enforces it;
  elsewhere restrict the shell too, with `bash: deny` or scoped allows such as
  `bash(rg *): allow`, which make Bash an allowlist.
- `network` (`allow`, `deny`, `allowlist` with `network-allow` / `network-deny` hosts)
  governs shell connections only; web search and fetch are `tools: web`. Unset, it
  denies under `read-only` and `workspace-write`, allows under `danger-full-access`, and
  stays harness-native when `sandbox` is unset too.
- Only Claude and Codex enforce `sandbox` and `network`, each with gaps: on Linux,
  Claude's deny leaves the sandbox's own loopback reachable, Codex always leaves local
  services reachable, and Codex cannot put a `read-only` sandbox online. OpenCode and
  Pi enforce neither. Cursor's sandbox runs `read-only` as `workspace-write` and
  `workspace-write` unsandboxed, with network open.
- A sandboxed agent cannot spawn a child with a weaker sandbox or wider network, and a
  child counts by what its harness enforces, so one on OpenCode or Pi counts as
  unsandboxed. Give a sandboxed lead's children a boundary at least as tight as its
  own, on Claude or Codex.
- Nobody can answer an `ask` rule in a spawn, so it is refused at once (Codex and
  Cursor ignore `ask` altogether).
- `approval` is the fallback for calls `tools` doesn't decide; it never widens `tools`,
  and the same value means different things per harness.
- Meridian grants its own read and completion commands on every launch, and
  `meridian spawn` to spawn-capable profiles; don't list them in `tools`.

Trust the dry run's `Permissions:` report over the frontmatter: it names each rule the
chosen harness enforces, approximates, or drops. Meridian's
[permissions by harness](https://github.com/haowjy/meridian-cli/blob/main/docs/agent-profiles.md#permissions-by-harness)
has the full table. When a delegated agent is refused access, read
`meridian skills escalation__meridian` before changing its profile.
