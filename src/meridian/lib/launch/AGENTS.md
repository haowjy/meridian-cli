# lib/launch/

Composition and execution layer: turns a `SpawnRequest` into a running subprocess,
then records what happened. Sits between the policy layer (`ops/spawn/`) and
mechanism layer (harness adapters, state stores).

## Entry Points

- `__init__.py` — `launch_primary()`: interactive primary-session launch
- `context.py` — `build_launch_context()`, `prepare_launch_surface()`,
  `bind_launch_context()`: the sole composition seam (invariant I-1)
- `streaming_runner.py` — `execute_with_streaming()`: async executor for
  spawn/streaming-serve paths
- `process/` — `run_harness_process()`: PTY/pipe executor for the primary path

## Key Types

- `SpawnRequest` / `LaunchRuntime` — frozen Pydantic DTOs; caller intent + adapter inputs
- `PreparedLaunchSurface` — expensive resolution output; the prepare/bind boundary
- `RuntimeBindings` — spawn-ID and runtime-only values fed to `bind_launch_context()`
- `LaunchContext` — fully composed launch state; complete at construction

## Module Map

| File | Purpose |
|---|---|
| `context.py` | `build_launch_context()` (backward-compat wrapper), `prepare_launch_surface()`, `bind_launch_context()`, `PreparedLaunchSurface`, `RuntimeBindings` |
| `request.py` | `SpawnRequest`, `LaunchRuntime`, `LaunchArgvIntent`, `LaunchCompositionSurface` |
| `plan.py` | `build_primary_spawn_request/runtime()` — primary-path input builders |
| `composition.py` | `ComposedLaunchContent`, `ProjectedContent` — semantic IR types |
| `policies.py` | `resolve_policies()` → `ResolvedPolicies` |
| `permissions.py` | `resolve_permission_pipeline()` |
| `command.py` | `resolve_launch_spec_stage()`, `build_launch_argv()` |
| `fork.py` | `materialize_fork()` — sole callsite for `adapter.fork_session()` |
| `prompt.py` | Prompt composition for SPAWN_PREPARE path; goal instruction rendering |
| `resolve.py` | `resolve_skills_from_profile()`, harness resolution |
| `reference.py` | `load_reference_items()`, template variable resolution |
| `env.py` | `build_env_plan()`, `build_harness_child_env()` |
| `extract.py` | `enrich_finalize()`: usage + session + report extraction |
| `signals.py` | `SignalForwarder`, `SignalCoordinator`; SIGINT/SIGTERM forwarding |
| `process/` | PTY and pipe launchers; primary-path subprocess execution |
| `streaming/` | Heartbeat and terminal arbitration for streaming paths |

## Depth Reference

- `.context/CONTEXT.md` — composition invariants, prepare/bind split rationale,
  four-adapter architecture, DTO discipline, workspace projection, env injection

## Related

- `../ops/spawn/` — policy layer calling into launch
- `../harness/` — adapters that launch calls into (lateral link)
- KB `architecture/launch-system.md` — full four-adapter diagram, invariants, module map
- KB `concepts/spawn-lifecycle.md` — spawn status machine, crash recovery, authority lattice
