---
name: prompt-authoring__meridian
description: When writing or reviewing agent profiles or skills for Meridian.
metadata:
  mode: primary
---

# Prompt Authoring

An agent is one markdown file: YAML frontmatter for routing and policy, then the body,
which becomes its system prompt. A skill is a folder whose `SKILL.md` holds frontmatter
and the text that loads. Mars compiles both into `.mars/`, and Meridian launches from
there. Never edit `.mars/` or the copies sync writes into harness directories
(`.claude/skills/`, `.pi/skills/`, ...): the next sync overwrites them.

## The Loop

1. **Edit the source.** A project's own items are `.mars-src/agents/NAME.md` and
   `.mars-src/skills/NAME/SKILL.md`, committed with the project; a package keeps them in
   `agents/` and `skills/` at its root. Keep the file or folder name equal to the
   frontmatter `name`. With no `mars.toml` yet, run `meridian mars init`. Link each
   harness whose skill directory should receive skills with `meridian mars link .claude`.
   Linked targets also limit which harnesses can launch (`resources/models.md`).
2. **Check.** `meridian mars check .mars-src` (bare `meridian mars check` in a package
   root) fails on what sync only warns about and then drops: an invalid field value, a
   malformed `model-policies`, a skill without a `description`, unparseable YAML. A
   description containing `: ` must be quoted.
3. **Sync.** `meridian mars sync` installs into `.mars/` and copies skills into linked
   harness directories. Use it rather than bare `mars sync`, which also writes
   harness-native agent copies that compete with Meridian's routing.
4. **Preview.** `meridian spawn -a NAME --dry-run -p "TASK"` prints the harness command
   and a `Permissions:` report for the harness the model routes to;
   `meridian -a NAME --dry-run` previews a primary launch. On Pi and Codex the command
   carries the whole composed system prompt. Claude reads it from a file the dry run
   doesn't write, so to read it, preview with a Pi or Codex model (`-m`). Read the
   prompt as the agent will: the body, the loaded skills, what Meridian added, and every
   rule reported `partial` or `unsupported`. Try a skill on an existing agent with
   `--skills NAME`.
5. **Run it.** Give the agent one real task on a cheap model and read what it does; a
   prompt that reads well can still steer badly.

## Resources

- `resources/agents.md`: writing or reviewing a profile: what callers see, what Meridian
  adds to the prompt, what reaches a spawn, and what each harness enforces.
- `resources/skills.md`: writing or reviewing a skill: the folder, how it reaches an
  agent, invocability, and types.
- `resources/models.md`: choosing an agent's model and backups, writing
  `model-policies`, or a launch that finds no usable model.

These cover the formats and how Meridian and Mars treat them. If a prompt-writing skill
is available, load it for the craft of the text itself.
