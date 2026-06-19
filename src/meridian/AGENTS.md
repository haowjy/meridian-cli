# src/meridian/ — Package Intent

Meridian is a coordination layer, not an execution engine. It launches agents,
observes their state, and surfaces results. It does not control how agents do
their work.

## Layered Architecture

The package is structured in concentric layers. Surfaces (CLI, MCP server, REST)
call into policy (`ops/`), which drives mechanism (`launch/`, `harness/`),
which reads/writes state (`state/`). Data flows inward; nothing from mechanism
calls back into surfaces.

```
cli/ · server/ · plugin_api/   ← external surfaces (user-facing, stable)
        │
        ▼
lib/ops/                       ← policy: what to do, access control, validation
        │
        ├──→ lib/launch/       ← composition + execution: how to assemble and run
        │         │
        │         └──→ lib/harness/   ← mechanism: per-harness adapters
        │
        └──→ lib/state/        ← persistence: all disk I/O, atomic writes
```

`lib/` is internal — callers inside `meridian` are welcome, external plugins are not.
`plugin_api/` is the stable external surface — built-in hooks and external plugins
import only from here. If a hook needs something not in `plugin_api/`, extend the
API, don't reach into `lib/`.

## Key Mental Model

**Everything important is on disk.** No in-memory-only state survives process death.
Spawns, sessions, work items, and artifacts are JSONL or JSON files. If it's not
on disk, it doesn't exist.

**Harness-agnostic at the ops/launch boundary.** `ops/` and `launch/` work with
`SpawnRequest`, `LaunchContext`, and domain types — not Claude- or Codex-specific
structures. Harness specifics are confined to `harness/`.

**Adding a harness, source, or command stays isolated.** New harness = one adapter
file + registration. New CLI command = one module. New package source = one mars
config entry. If adding a feature requires editing 10 files, the abstraction is wrong.

## Lib Subpackage Index

| Subpackage | What it owns |
|---|---|
| `lib/ops/` | Policy: spawn lifecycle, session, work, config operations |
| `lib/launch/` | Composition seam: `SpawnRequest` → `LaunchContext` → process |
| `lib/harness/` | Per-harness adapters: Claude, Codex, OpenCode |
| `lib/state/` | All disk I/O: spawn store, session store, atomic writes |
| `lib/core/` | Domain types, spawn lifecycle state machine |
| `lib/platform/` | OS abstractions: locking, process scope, signals |
| `lib/catalog/` | Agent/skill catalog, profile resolution |
| `lib/config/` | Config loading and layering |
| `lib/spawn/` | Archive overlay: UI visibility flag for terminal spawns |
| `lib/streaming/` | Harness output streaming, event parsing |
| `lib/safety/` | Budget enforcement, guardrails, secret redaction |
| `lib/context/` | Context resolution (KB, strategy, work directories) |
| `lib/hooks/` | Hook dispatch, config layering, built-in hooks |
| `lib/extensions/` | Extension command registry |

| `lib/observability/` | Spawn-scoped JSONL tracing |
| `lib/mermaid/` | Mermaid diagram validation |
| `lib/kg/` | Markdown link checking |
| `lib/bootstrap/` | Startup preparation stages |
| `lib/markdown/` | Markdown parsing utilities |

## Entry Points

- `__main__.py` — `python -m meridian`
- `cli/entrypoint.py` — `main()` for the installed CLI binary
- `server/main.py` — `run_server()` for `meridian serve`

## Logging Convention

Catalog/config modules use stdlib `logging.getLogger(__name__)`. Ops/launch/harness
modules use `structlog.get_logger()`. The split is load-bearing — `capture_library_diagnostics()`
captures stdlib warnings during spawn; structlog bypasses it and leaks to stderr if used
in the wrong modules.

## Anti-Patterns

**Don't reach into `lib/` from external plugins.** Extend `plugin_api/` instead.
`lib/` has no stability guarantees.

**Don't add harness-specific logic to `ops/` or `launch/`.** If something only applies
to Claude or Codex, it belongs in `harness/`.

**Don't mix composition surfaces.** The four driving adapters (primary CLI, spawn subprocess,
REST app, streaming-serve) each have a defined entry point into `launch/`. Adding a fifth
path without understanding how finalization ownership works will produce orphaned spawns.

## Related

- `CLAUDE.md` → `AGENTS.md` — safety rules, design constraints, dev commands
- `.context/CONTEXT.md` — design philosophy, logging, cross-platform paths
- `lib/launch/AGENTS.md` — composition seam; four driving adapters
- `lib/harness/AGENTS.md` — translation pipeline; SpawnParams accounting
- `lib/state/AGENTS.md` — file layout; atomic write invariants
- `lib/ops/AGENTS.md` — policy layer; what belongs here vs launch/
