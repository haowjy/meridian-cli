# lib/core/ — Shared Primitives

Typed IDs, config merge, runtime context, spawn lifecycle, and `SpawnApplicationService`
(the one spawn application seam for CLI and streaming-serve). Most lib packages import
from here, so keep pure types dependency-light; `spawn_service.py` and related modules
intentionally compose `launch/`, `state/`, `harness/`, and `streaming/`.

## Rules

- **`SpawnStatus` (`domain.py`) is the single status authority.** Use the derived sets
  (`ACTIVE_SPAWN_STATUSES`, `TERMINAL_SPAWN_STATUSES`, …); never create a parallel status
  vocabulary or frozenset. `SpawnKind` lives in `plugin_api/types.py`; extend that literal.
- **Use `SpawnId`, `ModelId`, `ChatId`, `HarnessSessionId`, not `str`.** Persisted chat and
  harness-session fields use the `Annotated` aliases that normalize blanks to `None`.
- **`RuntimeOverrides`: `None` means "not set", never `""` or `0`.** A non-None value at a
  high-precedence layer wins, including the empty string.
- **The environment registry is the sole declaration of child-process Meridian keys.**
  Don't add child env keys outside the bind seam; register every key before projecting it
  (`validate_child_env_keys()` rejects unknown names). Build `ResolvedContext` with
  `from_environment()`, not dict literals.
- **Every creation form (create, continue, fork) enters one caller-boundary path** before
  operation-specific state: `caller_boundary.py` owns the checks, `ops/spawn/api.py` the
  shared entry.
- **Spawn preparation materializes the bound launch before persistence**: adapter-owned
  writes finish (`launch.context.materialize_launch_context()`) before
  `SpawnApplicationService` creates the row.
- **`is_root_side_effect_process()` fails closed**: a malformed non-empty
  `_MERIDIAN_DEPTH` returns `False`. Root-only side effects (the reaper) must not run in
  delegated agent processes.
- **Dead-launch finalization has one writer**, `finalize_dead_launch()`; see
  [../state/AGENTS.md](../state/AGENTS.md).
- **`LifecycleHook.on_event()` exceptions are logged, never block transitions.**
- **`lifecycle.py` imports `spawn_store` lazily** (`_spawn_store()`) to break a cycle with
  `state/__init__`; keep it lazy.

[.context/CONTEXT.md](.context/CONTEXT.md) — ID types, override merge, `OutputSink`,
depth, child env, lifecycle transitions and events, cleanup policy.
