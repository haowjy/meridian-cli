# lib/core — Contracts and Architecture

## ID Types (`types.py`)

`SpawnId`, `ModelId`, `ArtifactKey`, `SchemaVersion` are `NewType` aliases
over `str` / `int`. Zero runtime cost; mypy catches pass-the-wrong-id bugs
statically. Use them on function signatures wherever spawn or model IDs are
passed — don't accept `str` when you mean `SpawnId`.

`HarnessId` and `TransportId` are defined here; the former `harness/ids.py` shim was deleted.

## RuntimeOverrides and Merge Semantics (`overrides.py`)

`RuntimeOverrides` is a frozen Pydantic model. Every field is `T | None`
where `None` means "not set at this layer."

```python
resolve(cli_overrides, env_overrides, profile_overrides, config_overrides)
```

Returns first-non-None per field across N layers. Constructors:
`from_env()`, `from_agent_profile()`, `from_config()`, `from_spawn_config()`,
`from_spawn_input()`, `from_launch_request()` — one per precedence level.

**Do not default to `""` or `0` to signal "not set" — use `None`.** A
non-None value at any layer wins, even if it's the empty string.

## OutputSink Protocol (`sink.py`)

```python
class OutputSink(Protocol):
    def result(self, payload) -> None: ...
    def status(self, message) -> None: ...
    def warning(self, message) -> None: ...
    def error(self, message, exit_code=1) -> None: ...
    def heartbeat(self, message) -> None: ...
    def event(self, payload) -> None: ...
```

Callers accept a sink, not a file path. `NullSink` discards everything.
Swapping sink type changes output routing without touching caller code.
Background paths and tests pass `NullSink`.

## Depth Parsing and Root-Side-Effect Gating (`depth.py`)

`MERIDIAN_DEPTH` propagates through spawned processes. The reaper and other
root-only side effects must only run at depth 0.

`is_root_side_effect_process()` is **fail-closed**: a malformed non-empty
value returns `False`. A root-only side effect (e.g., the reaper
auto-finalizing spawns) must not run inside a delegated agent process.
Malformed `MERIDIAN_DEPTH` indicates corruption, test isolation, or
unexpected nesting — failing closed prevents incorrect reap actions.

Other helpers: `current_meridian_depth()`, `child_meridian_depth()`,
`is_nested_meridian_process()`, `max_depth_reached()`.

## ResolvedContext and Child Env (`resolved_context.py`, `child_env.py`)

`ResolvedContext` is the authoritative runtime context for one process,
built from `MERIDIAN_*` env vars. `ResolvedContext.from_environment()` is
the canonical constructor — do not build it from dict literals in
application code.

`child_env_overrides(*, increment_depth, child_spawn_id)` is the **only**
correct way to produce child-process `MERIDIAN_*` env vars. All launch
paths that build child env must route through it.

`ALLOWED_CHILD_ENV_KEYS` frozenset enforces the allowed key set.
`validate_child_env_keys()` raises on unknown keys. `MERIDIAN_CONTEXT_<NAME>_DIR`
keys are validated by regex pattern.

## SpawnLifecycleService (`lifecycle.py`)

State transitions with post-write observer dispatch:

```
start()          → spawn.created
mark_running()   → spawn.running
record_exited()  → (no event — sets exit code only)
mark_finalizing() → CAS running→finalizing (no event)
finalize()       → spawn.finalized
cancel()         → finalize(status='cancelled')
```

`LifecycleHook.on_event(event)` — exceptions are logged but never block
transitions. Hooks must be tolerant of partial data.

`LifecycleEvent` UUID v5 `event_id` is stable per (spawn_id, event_type)
— safe to deduplicate on re-delivery.

**Import note:** `spawn_store` is imported at module bottom to break a
circular import with `state/__init__`. Do not move it to the top.

## Clock Protocol (`clock.py`)

`Clock` protocol (`monotonic()`, `time()`, `utc_now_iso()`) is injected
into spawn_store, streaming_runner, and reaper. Tests control time without
patching globals. Production code always receives `RealClock`.

## Related KB

→ [KB: codebase/core-primitives.md](/home/jimyao/.meridian/git/meridian-flow-docs/kb/codebase/core-primitives.md)
→ [KB: concepts/config-precedence.md](/home/jimyao/.meridian/git/meridian-flow-docs/kb/concepts/config-precedence.md)
→ [KB: architecture/state-system.md](/home/jimyao/.meridian/git/meridian-flow-docs/kb/architecture/state-system.md)
