# Configuration

Hook configuration provenance follows the highest layer defining the section,
while the hooks owner aggregates rows across layers, including when a higher
section is empty. Workspace configuration is owned and merged separately.

Claude resolves its spawn-watch option from the control root; native agent
routing follows Mars-installed agents rather than project config.

KB `mars/model-resolution.md` ("Policies and fallback") explains how routing
and per-launch policy layer over project config.
