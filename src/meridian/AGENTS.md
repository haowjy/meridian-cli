# src/meridian/ — Package Intent

Meridian is a coordination layer, not an execution engine. It launches agents,
observes their state, and surfaces results. It does not control how agents do
their work. Subpackage index and entry points: `.context/CONTEXT.md`.

## Layers

Data flows inward; nothing in mechanism calls back into a surface.

```
cli/ · server/ · plugin_api/   ← surfaces
        │
        ▼
lib/ops/                       ← policy: what to do, access control, validation
        │
        ├──→ lib/launch/       ← composition + execution
        │         └──→ lib/harness/   ← mechanism: per-harness adapters
        └──→ lib/state/        ← persistence: all disk I/O, atomic writes
```

`cli/` also imports `lib/launch` directly (primary launch). `lib/` is internal with no
stability guarantee: `plugin_api/` is the only surface external plugins may import.
Built-in hooks take general capabilities from `plugin_api/` too, with a few named
companion imports from `lib/` (`lib/hooks/AGENTS.md`); if a hook needs a general
capability the API lacks, extend the API.

## Rules

- **Everything important is on disk.** No in-memory-only state survives process death.
- **Harness-agnostic at the ops/launch boundary.** `ops/` and `launch/` work with
  `SpawnRequest`, `LaunchContext`, and domain types; Claude/Codex specifics stay in `harness/`.
- **Don't mix composition surfaces.** Three driving adapters (primary CLI, spawn
  subprocess, streaming-serve) each enter `launch/` at a defined point. A fourth path
  that ignores finalization ownership orphans spawns.
- **Pick the logger by where the message should go.** Inside
  `capture_library_diagnostics()` (launch boundaries, `lib/diagnostics.py`), stdlib
  `logging` records from `meridian.lib.*` below ERROR are collected as launch diagnostics
  and kept off stderr; structlog always reaches stderr. Use stdlib for warnings that should
  become launch diagnostics (catalog/config, idle sidecar), structlog for operator-visible logs.

## Typing

Pyright strict is the gate, warnings included (`pyright --warnings`, 0/0). Fix
diagnostics at the source; never demote a rule in `pyproject.toml`.

- **Suppress one rule per line, with a reason:** `# pyright: ignore[rule]  # reason`.
  `# type: ignore` is disabled because pyright treats it as blanket suppression. Stale
  ignores fail the gate. Reserve suppressions for untyped or private third-party APIs.
- **Narrow decoded JSON with `lib/utils/json_shapes.py`** (`json_object`, `json_list`,
  `loads_json_object`), not `cast` or a new private `isinstance` helper. Fixed-shape
  payloads parse through a Pydantic model or a `TypeAdapter`-validated TypedDict.
- **Cross-module names are public**: drop the underscore once another module imports it.
  Third-party private access sits behind one adapter module (`cli/cyclopts_internals.py`).

Rationale: KB `decisions/static-typing.md`.

## Related

`lib/launch/AGENTS.md` (composition seam), `lib/harness/AGENTS.md` (SpawnParams
accounting), `lib/state/AGENTS.md` (file layout, atomic writes), `lib/ops/AGENTS.md`
(policy vs launch).
