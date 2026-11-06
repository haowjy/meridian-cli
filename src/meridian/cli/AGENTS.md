# CLI startup

Give each real subcommand an exact `COMMAND_CATALOG` descriptor. Longest-prefix
fallback otherwise makes a missing `spawn` subcommand inherit the parent's
bootstrap and telemetry policy.

Keep handler imports local and command implementations out of `app_tree.py`:
selective registration depends on importing only the requested group. Normalize
argv before classification, including fork and continue forms.

For `-C` to another project, clear project-scoped inherited environment for the
whole invocation; attribution from the source project cannot cross that boundary.
The sender for `spawn message` is carried separately, not restored to the environment.

Session browse returns a re-entry decision; the handler execs it only after the
full-screen app closes. Build help profiles per invocation rather than mutating
shared `App` objects.

[.context/CONTEXT.md](.context/CONTEXT.md) describes the startup pipeline.
