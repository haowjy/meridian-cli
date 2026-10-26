# Skills

A skill is a folder: `SKILL.md`, with frontmatter and the text that loads, plus optional
`resources/`, `scripts/`, and `assets/`. Mars's [skill compilation reference](https://github.com/meridian-flow/mars-agents/blob/main/docs/config/skill-compilation.md)
has every field and how each harness receives it; this covers what the schema doesn't
show.

## The Folder

`name` and `description` are required once a `SKILL.md` has frontmatter. Keep the folder
name equal to `name`: Mars installs under the folder name but lists the frontmatter
name, so a mismatch gives one skill two names.

Write resource references relative to the skill (`resources/x.md`). A loaded skill
arrives with a line naming its installed folder, and the agent opens resources from
there; nothing beyond `SKILL.md` loads until the agent opens it. Keep `SKILL.md` to what
every activation needs, move depth into resources, and give each resource a line saying
when to open it.

## How a Skill Reaches an Agent

- **`skills.load`** in a profile (or a flat `skills:` list) puts the full text in that
  agent's system prompt on every launch and every harness, whatever its
  `model-invocable`.
- **`--skills NAME`** on a launch does the same for that run. A caller learns what it can
  attach from the agent's description.
- **`skills.available`** lists the name and description under `# Available Skills`,
  grouped by type, for the agent to load itself through the harness's skill tool.
- **The harness's own list** offers every model-invocable skill in its skill directory to
  any model running there. `meridian mars sync` fills that directory only for linked
  targets (`meridian mars link .claude`). Without a linked target for the running
  harness, the harness has no copy, and an `available` skill is a name its skill tool
  reports unknown.
- **`model-invocable: false`** takes the skill off the harness's list. `load` and
  `--skills` still deliver it in full, but an `available` entry for it can't be loaded.
  `false` plus one profile's `load` gives that agent a skill no model finds by itself.
  OpenCode ignores the field.
- **`user-invocable`** (default `true`) lets the human run `/NAME`; only Claude honors
  `false`.

Every model-invocable skill costs its description on every turn of every session, so
keep that pool small. The description is what an agent reads to decide whether to load
the skill: open with the situation that calls for it ("Use when ...", "Load when ...").

A loaded skill is headed `# Skill: /NAME` with a line saying its text is already loaded,
so a body can tell the agent to follow `/NAME` even when the harness's list omits it.
Under the experimental `experimental.skill_delivery = "named"` setting, Meridian instead
gives one line per loaded skill naming `/NAME` and the path to its `SKILL.md`, under the
same type headers.

## Types

`type:` places a loaded skill and prints a line above its group:

| `type` | Line printed | Use for |
|---|---|---|
| `principle` | "Override other guidance when loaded." | always-loaded thinking guidance |
| `guardrail` | "Load before acting in sensitive areas." | always-loaded safety boundaries |
| `mode-shift` | "Change how you operate when loaded." | on-demand shifts in activity |
| `checkpoint` | "Load at decision points to verify before continuing." | phase gates |
| `reference` (default) | none | operational how-to |

Principles come first, then guardrails, then everything else. Any other value is
accepted, gets a bare heading, and sorts with `reference`. Pick the type whose line holds
for the skill.

## Other Behavior

- `tools` pre-approves tools while the skill runs. Its grants never restrict anything,
  and only Claude and Pi apply them.
- `variants/HARNESS/SKILL.md` replaces the body on one harness; frontmatter always
  comes from the base file. Model-level variant folders are ignored.
- A project skill with a package skill's name replaces it, and sync warns that it
  shadows it. Two packages shipping one name both install as `NAME__SOURCE`, and agents'
  `skills:` references are rewritten to match.
- Meridian's own `NAME__meridian` skills ship with the CLI. They reach agents through
  Meridian's trigger list and `meridian skills NAME`, not through `skills:` or
  `--skills`.

If a prompt-writing skill is available, load it for how to design the skill itself.
