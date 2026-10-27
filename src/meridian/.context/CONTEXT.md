# src/meridian — Context

Orientation lookup; the rules an agent must follow are in `../AGENTS.md`.

## Lib Subpackage Index

| Subpackage | What it owns |
|---|---|
| `lib/ops/` | Policy: spawn lifecycle, session, work, config operations |
| `lib/launch/` | Composition seam: `SpawnRequest` → `LaunchContext` → process |
| `lib/harness/` | Per-harness adapters: Claude, Codex, Cursor, OpenCode, Pi |
| `lib/state/` | All disk I/O: spawn store, session store, atomic writes |
| `lib/core/` | Domain types, spawn lifecycle state machine |
| `lib/platform/` | OS abstractions: locking, process scope, signals |
| `lib/catalog/` | Agent profile and model catalog resolution |
| `lib/config/` | Config loading and layering |
| `lib/streaming/` | Harness output streaming, event parsing |
| `lib/safety/` | Budget enforcement, guardrails, permission resolution |
| `lib/context/` | Context resolution (KB, strategy, work directories) |
| `lib/hooks/` | Hook dispatch, config layering, built-in hooks |
| `lib/extensions/` | Extension command registry |
| `lib/artifact/` | Detached static-artifact serving over Tailscale (`artifact serve/list/stop/gc`); no daemon — file is authority |
| `lib/notify/` | Harness-agnostic notification labels, delivery policy, and channel registry |
| `lib/idle/` | Harness-agnostic idle timeline, guards, persisted state machine, and sensor sidecar |
| `claude_runtime/` | Bundled Claude Code runtime mods; idle and enabled spawn-watch plugins are injected only for interactive launches |
| `pi_runtime/` | Bundled Pi extensions; `meridian-idle` is primary-only and `meridian-permissions` is policy-triggered |
| `lib/observability/` | Spawn-scoped JSONL tracing |
| `lib/telemetry/` | Process telemetry routing, sinks, lifecycle correlation, and retention |
| `lib/mermaid/` | Mermaid syntax validation and style checks |
| `lib/kg/` | KB document-graph analysis (links, conflicts, flags) |
| `skills/` | Meridian-owned guidance bundled in the wheel, provided into project `.mars/skills/`, and loaded on demand with `meridian skills` |
| `lib/bootstrap/` | Startup preparation stages |
| `lib/markdown/` | Markdown parsing utilities |
| `lib/utils/` | Dependency-free shared utility helpers |

## Entry Points

- `__main__.py` — `python -m meridian`
- `cli/entrypoint.py` — `main()` for the installed CLI binary
- `server/main.py` — `run_server()` for `meridian serve`
