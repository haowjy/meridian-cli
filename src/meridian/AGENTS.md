# Meridian package

`plugin_api/` is the stable surface for external plugins. Built-in hooks use it
for general capabilities too; their tightly coupled companions are listed in
`lib/hooks/builtin/AGENTS.md`.

Each driving adapter enters `launch/` at its own composition point. A new path
needs explicit finalization ownership or it can orphan spawns.

Within `capture_library_diagnostics()`, stdlib warnings from `meridian.lib.*`
become launch diagnostics; structlog reaches stderr. Choose the logger according
to where the warning belongs.

Pyright suppressions name one rule and a reason. Shared decoded JSON shapes use
`lib/utils/json_shapes.py`; fixed shapes use a Pydantic model or validated
TypedDict. KB `codebase/guide.md` explains the typing policy.
