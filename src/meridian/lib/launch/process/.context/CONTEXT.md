# launch/process — Contracts and Architecture

## Backend Selection

`select_process_backend()` returns a `SelectedProcessLauncher` with an explicit
`ProcessPlatformContract` describing how IO is surfaced.

Selection order when output capture is requested (`output_log_path` is set):
1. `PtyProcessLauncher` (PTY_MEDIATED, captures to artifact) — if `can_use_pty()`
2. `SubprocessProcessLauncher` (PIPE_CAPTURE, captures to artifact) — fallback

Selection order when no capture is needed (`output_log_path` is None):
1. `WindowsConsoleLauncher` (NATIVE_INHERIT, no capture) — if `can_use_windows_console_launcher()`
2. `PtyProcessLauncher` (PTY_MEDIATED, no capture) — elif `can_use_pty()`
3. `SubprocessProcessLauncher` (NATIVE_INHERIT, no capture) — fallback

The PTY launcher is preferred for POSIX interactive TUI harnesses. Windows console launcher
handles Windows-specific console inheritance. Subprocess is the portable fallback.

`captures_output_to_artifact` on `ProcessPlatformContract` is authoritative — callers must
not assume capture based on launcher type alone.

## Managed Attach vs Black-Box

`_execute_primary_process()` checks `harness_contract.bootstrap.mode`. When mode is
`managed_primary_attach`:

1. Calls `run_primary_attach()` → `PrimaryAttachLauncher`
2. On `PrimaryAttachError`: if `primary_attach_failure_policy == "raise"`, re-raises; otherwise
   logs a warning, deletes managed sidecars (`PRIMARY_META_FILENAME`, `OUTPUT_FILENAME`,
   `stderr.log`), and falls back to black-box
3. Black-box: calls `run_primary_process_with_capture_fn()` directly

Managed attach persists the harness session ID from `PrimaryAttachOutcome.session_id`
immediately on success. Black-box path may discover the session ID only at exit via
`observe_session_id()`.

## Session ID Observation — Invariant I-4

`harness_adapter.observe_session_id()` is called **exactly once** after the process exits,
inside `_finalize_lifecycle_and_observe_session()`. It discovers the harness session ID from
artifacts (history.jsonl, output.jsonl) written during execution.

If the observed ID differs from what was persisted at launch, a warning is logged and the
store is updated. If observation fails, it is swallowed — session ID persistence is
best-effort after exit. Do not call `observe_session_id()` at any other point in the lifecycle.

## Finalization Ownership

`_finalize_lifecycle_and_observe_session()` is called in a `finally` block inside
`run_harness_process()`. It is responsible for:
- Calling `spawn_service.complete_execution()` with `ExecutionTerminalFacts`
- Resolving the final exit code (may differ from process exit code for graceful report-completion)
- Persisting observed harness session ID
- Calling `harness_adapter.cleanup_prelaunch()`

`complete_execution()` is idempotent — safe to call on a spawn already in terminal state.

## run_harness_process() Caller Contract

Callers must provide a fully resolved `LaunchContext` with a valid `binding.argv`. The function:
1. Opens `session_scope` (creates session store entry)
2. Calls `lifecycle_service.start()` (creates spawn row, sets status to `queued`)
3. Materializes fork if `session_mode == FORK` and harness supports it
4. Rebuilds `LaunchContext` with real spawn ID and paths (or binds from `PreparedLaunchSurface`)
5. Calls `harness_adapter.prepare_prelaunch()` — env overrides applied to `child_env`
6. Executes process
7. Finalizes lifecycle in `finally`

The `prepared` argument carries a `PreparedLaunchSurface` from the prepare/bind split. When
present, `bind_launch_context()` is used instead of `build_launch_context()` — this is the
primary CLI's prepare-once/bind-twice optimization path.

## Lateral Links

→ [../../.context/CONTEXT.md](../../.context/CONTEXT.md) — composition seam, four driving adapters, invariants I-1/I-4/I-10
→ [../../streaming/.context/CONTEXT.md](../../streaming/.context/CONTEXT.md) — streaming spawn execution (sibling path)
