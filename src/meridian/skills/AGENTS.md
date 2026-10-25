# Bundled Meridian skills

These skills ship with the binary whose behavior they explain. Unlike prompt-package
skills, they should name the real Meridian commands and the flags needed to act; keep
that syntax aligned with the command help. When trimming, cut flag syntax that only
restates `-h`, and keep the judgment: what to try first, which fix fits which symptom,
why, and what to tell the human. Likewise, where a skill explains a Mars file format,
link Mars's reference for the field schema and keep only what the schema doesn't show.

Every `SKILL.md` requires `name` and a trigger-shaped `description` beginning with
"When". Optional `mode: primary` limits its trigger line to profiles whose declared
mode is `primary`; omit `mode` when every profile should see it.
Bundled skill folders and frontmatter names end in `__meridian`, matching Mars's
`name__source` collision spelling. Bundled skills must not depend on package-specific
agent or skill names: describe the role conditionally and state any load-bearing rule
inline.
