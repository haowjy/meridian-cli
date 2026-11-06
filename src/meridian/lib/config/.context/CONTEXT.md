# Configuration

Hook configuration provenance follows the highest layer defining the section,
while the hooks owner aggregates rows across layers, including when a higher
section is empty. Workspace configuration is owned and merged separately.

Pi's launch profile resolves some environment overrides after choosing the
config snapshot. Claude resolves its spawn-watch option from the control root;
native agent routing follows Mars-installed agents rather than project config.

KB `concepts/config-precedence.md` describes the config layers and the distinction
between project config and per-spawn overrides.
