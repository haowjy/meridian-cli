# CLI startup

Give each real subcommand an exact `COMMAND_CATALOG` descriptor. Longest-prefix
fallback otherwise makes a missing `spawn` subcommand inherit the parent's
bootstrap and telemetry policy.

Keep handler imports local and command implementations out of `app_tree.py`:
selective registration depends on importing only the requested group. Normalize
argv before classification, including fork and continue forms.

For `-C` to another project, clear project-scoped inherited environment for the
whole invocation; attribution from the source project cannot cross that boundary.
The sender for `message` is carried separately, not restored to the environment.
A claimed ID reaches another project without `-C`, so nothing clears the caller's variables
for a command that only reads, waits on or signals it: code that keys state by the caller
goes through `lib/core/caller.py`, never `os.environ`, and a launch into another project
(`--continue`) re-runs under `-C` in `continue_reentry.py`.

A command that prints a JSON error of its own for failures raised before its handler
(`idle`, `message`) names the shape in its catalog descriptor's `startup_error`; `main`
asks the catalog, never the command name. A reply line or help text that sends a
message begins `meridian message`, with global options after the address: a harness lets
a command out of its sandbox by its first words.

Session browse returns a re-entry decision; the handler execs it only after the
full-screen app closes. Build help profiles per invocation rather than mutating
shared `App` objects.

[.context/CONTEXT.md](.context/CONTEXT.md) describes the startup pipeline.
