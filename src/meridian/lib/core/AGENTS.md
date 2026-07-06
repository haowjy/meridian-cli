# lib/core/ — Shared Primitives

Shared primitives and cross-surface spawn orchestration. Most lib packages import
from here; pure types and utilities stay dependency-light, but `spawn_service.py`
and related modules intentionally compose `launch/`, `state/`, `harness/`, and
`streaming/` so every surface shares one spawn application seam.

## Mental Model

Core provides: typed IDs, config merge semantics, runtime context, output routing,
spawn lifecycle state machine, process depth tracking, and `SpawnApplicationService`
(create/list/cancel/prepare spawns across CLI and streaming-serve). Domain types and
merge helpers are harness-agnostic substrate; spawn application code is the
deliberate exception that wires policy surfaces to launch and persistence.

## Key Rules

- **Use `SpawnId`, `ModelId`, `ArtifactKey` — not `str`.** These are `NewType` aliases over `str`. Zero runtime cost; mypy catches wrong-ID bugs statically. Accept them on function signatures anywhere a spawn or model ID passes through.
- **`RuntimeOverrides`: use `None` to mean "not set", never `""` or `0`.** `resolve(*layers)` returns first-non-None per field. An empty string at a high-precedence layer wins over a meaningful value at a lower one.
- **`child_env_overrides()` is the only correct way to produce child-process `MERIDIAN_*` env vars.** Every launch path routes through it. `ALLOWED_CHILD_ENV_KEYS` enforces the allowed set — `validate_child_env_keys()` raises on unknown keys.
- **`is_root_side_effect_process()` is fail-closed.** A malformed non-empty `MERIDIAN_DEPTH` returns `False`. Root-only side effects (e.g., the reaper) must not run inside delegated agent processes. When in doubt, fail closed.
- **`LifecycleHook.on_event()` exceptions are logged but never block transitions.** Hooks must tolerate partial data.
- **`spawn_store` is imported at the bottom of `lifecycle.py`** to break a circular import with `state/__init__`. Do not move it to the top.

## Entry Points

- `types.py` — `SpawnId`, `ModelId`, `ArtifactKey`, `SchemaVersion` (typed IDs)
- `overrides.py` — `RuntimeOverrides`, `resolve(*layers)` (config precedence merge)
- `resolved_context.py` — `ResolvedContext.from_environment()` (authoritative runtime env for one process)
- `child_env.py` — `child_env_overrides()`, `ALLOWED_CHILD_ENV_KEYS`, `validate_child_env_keys()`
- `lifecycle.py` — `SpawnLifecycleService` (state machine: created → running → finalizing → finalized)
- `spawn_service.py` — `SpawnApplicationService` (create/list/cancel spawns)
- `sink.py` — `OutputSink` protocol, `NullSink` (output routing abstraction)
- `depth.py` — `is_root_side_effect_process()`, `child_meridian_depth()`
- `clock.py` — `Clock` protocol, `RealClock` (injectable time for tests)
- `domain.py` — `Spawn` domain model

## Spawn Lifecycle Transitions

```
start()           → spawn.created
mark_running()    → spawn.running
record_exited()   → (sets exit code, no event)
mark_finalizing() → CAS running→finalizing (no event)
finalize()        → spawn.finalized
cancel()          → finalize(status='cancelled')
```

`LifecycleEvent` uses UUID v5 `event_id` stable per `(spawn_id, event_type)` — safe to deduplicate on re-delivery.

## Anti-Patterns

- Don't default override fields to `""` or `0` — use `None`. A non-None value wins at any layer, including the empty string.
- Don't build `ResolvedContext` from dict literals in application code — use `ResolvedContext.from_environment()`.
- Don't produce `MERIDIAN_*` child env vars manually — route through `child_env_overrides()`.
- Don't pass raw `str` where `SpawnId` or `ModelId` is expected — mypy will catch it but it's also a clarity failure.

## Related

- `../state/` — spawn store consumed by `SpawnLifecycleService`
- `../harness/ids.py` — `HarnessId` (re-exported via core types)
- `../launch/` — uses `SpawnApplicationService` and `ResolvedContext`

→ [.context/CONTEXT.md](.context/CONTEXT.md) — ID type details, override merge semantics, `OutputSink` swap pattern, depth rationale, child env key enforcement
→ [KB: codebase/core-primitives.md](/home/jimyao/.meridian/git/meridian-flow-docs/kb/codebase/core-primitives.md)
→ [KB: concepts/config-precedence.md](/home/jimyao/.meridian/git/meridian-flow-docs/kb/concepts/config-precedence.md)
