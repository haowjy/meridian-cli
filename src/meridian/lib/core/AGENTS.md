# lib/core/

Shared primitives used by all subsystems. No business logic. Everything
else imports from here; core imports nothing from sibling lib packages.

## Entry Points

- `types.py` — `SpawnId`, `ModelId`, `ArtifactKey`, `SchemaVersion` (opaque NewType IDs)
- `overrides.py` — `RuntimeOverrides`, `resolve(*layers)` (config precedence merge)
- `sink.py` — `OutputSink` protocol, `NullSink`
- `depth.py` — `is_root_side_effect_process()`, `child_meridian_depth()`
- `resolved_context.py` — `ResolvedContext.from_environment()` (authoritative runtime env)
- `child_env.py` — `child_env_overrides()`, `ALLOWED_CHILD_ENV_KEYS`, `validate_child_env_keys()`
- `lifecycle.py` — `SpawnLifecycleService`, `LifecycleEvent`, `LifecycleHook`
- `spawn_service.py` — `SpawnApplicationService` (spawn create/list/cancel)
- `clock.py` — `Clock` protocol, `RealClock`
- `logging.py` — `configure_logging(json_mode, verbosity)` (call once at process start)
- `domain.py` — `Spawn` domain model
- `context.py` — `ExecutionContext` (context propagation carrier)
- `telemetry.py` — core telemetry helpers
- `util.py` — shared utilities

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) for:
- ID type usage and mypy enforcement
- `RuntimeOverrides` merge semantics and precedence layers
- `OutputSink` swap pattern
- `is_root_side_effect_process()` fail-closed rationale
- `child_env_overrides()` — canonical child-env emission path
- `SpawnLifecycleService` transitions and `LifecycleEvent` shape

## Related

- `../state/` — spawn store consumed by `SpawnLifecycleService`
- `../harness/ids.py` — `HarnessId` (re-exported via core types)
- `../launch/` — uses `SpawnApplicationService` and `ResolvedContext`
