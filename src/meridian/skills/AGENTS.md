# Bundled Meridian skills

These skills ship with the binary whose behavior they explain. Unlike prompt-package
skills, they should name the real Meridian commands and the flags needed to act; keep
that syntax aligned with the command help.

Every `SKILL.md` requires `name` and a trigger-shaped `description` beginning with
"When". Use `spawn-only: true` when the skill is meaningless without delegation.
Bundled skills must not depend on package-specific agent or skill names: describe the
role conditionally and state any load-bearing rule inline.
