# Test Audit

## Summary
- GOOD: 58 files
- MISCLASSIFIED: 2 files
- REWRITE: 14 files (by lane: A=2, B=2, C=0, D=2, E=3, F=1, G=4)
- DUPLICATE: 0 files

## Detailed Classification

### tests/unit/

| File | Classification | Lane | Reason |
|------|----------------|------|--------|
| harness/test_claude_slug.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| harness/test_claude_ws.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| harness/test_codex_ws.py | REWRITE | D | Monkeypatches private websocket internals (`_send_jsonrpc_error`) and private approval map. |
| harness/test_launch_spec.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| harness/test_lifecycle.py | REWRITE | D | Monkeypatches private connection methods (`_signal_process`, `_request`, `_close_ws`, `_post_session_action`). |
| harness/test_workspace_projection.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| launch/test_claude_cwd_isolation.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| launch/test_decision.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| launch/test_errors.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| launch/test_spawn_request_round_trip.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| ops/test_depth.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| ops/test_query.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| ops/test_spawn_log.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| prompt/test_compose.py | REWRITE | G | Uses real filesystem reads/writes in `tests/unit/`; should be rewritten around injectable seams/fakes. |
| server/test_mcp_schema.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| state/test_events.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| state/test_spawn_lifecycle.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| test_overrides_convention.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| test_passthrough_split.py | GOOD | — | Behavior-focused test with scope matching directory intent. |

### tests/integration/

| File | Classification | Lane | Reason |
|------|----------------|------|--------|
| catalog/test_agent.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| catalog/test_model_aliases.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| catalog/test_models.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| cli/test_cli_main.py | REWRITE | E | Monkeypatches private CLI helpers (`_run_mars_passthrough`, `_resolve_mars_executable`, `_execute_mars_passthrough`). |
| cli/test_cli_mars.py | REWRITE | E | Relies on private CLI internals (`_resolve_mars_executable`, `_interactive_terminal_attached`, `_run_mars_passthrough`). |
| cli/test_cli_spawn.py | REWRITE | E | Mixes integration scope with private CLI entrypoints and private terminal detection hook. |
| cli/test_primary_launch.py | MISCLASSIFIED | — | Pure branch logic test with monkeypatched dependency; no real integration boundary (belongs under unit). |
| config/test_project_config_ops.py | REWRITE | G | Monkeypatches private config module constant `_DEFAULT_USER_CONFIG`. |
| config/test_settings_paths.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| config/test_workspace.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| harness/test_adapter_ownership.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| harness/test_claude_session_symlink.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| harness/test_codex_fork_session.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| harness/test_extraction.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| harness/test_opencode_http.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| launch/test_app_agui_phase3.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| launch/test_app_server.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| launch/test_control_socket.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| launch/test_debug_tracer.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| launch/test_launch_process.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| launch/test_launch_resolution.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| launch/test_permissions.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| launch/test_signal_canceller.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| launch/test_signals.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| launch/test_spawn_inject.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| launch/test_spawn_manager.py | REWRITE | B | Monkeypatches private manager method `_cleanup_completed_session`. |
| launch/test_streaming_runner.py | REWRITE | B | Monkeypatches private runner hook `_report_watchdog`. |
| launch/test_streaming_serve.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| ops/test_diag.py | REWRITE | G | Monkeypatches private repair internals (`_repair_stale_session_locks`, `_repair_orphan_runs`). |
| ops/test_mars.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| ops/test_reference.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| ops/test_runtime.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| ops/test_session_log.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| ops/test_spawn_api.py | REWRITE | G | Monkeypatches private API helper `_resolve_project_root_input`. |
| ops/test_spawn_context_ref.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| ops/test_spawn_continue.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| ops/test_spawn_prepare_fork.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| ops/test_spawn_read_reconcile.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| ops/test_workspace_ops.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| state/test_event_store.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| state/test_liveness.py | MISCLASSIFIED | — | Pure logic with psutil stubs; no filesystem/process integration boundary (belongs under unit). |
| state/test_paths.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| state/test_project_paths.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| state/test_reaper.py | REWRITE | A | Monkeypatches private reconciliation helper `_collect_artifact_snapshot`. |
| state/test_session_safety.py | REWRITE | A | Monkeypatches private lock internals (`_acquire_session_lock`, internal lock maps). |
| state/test_spawn_store.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| state/test_state_layer.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| state/test_work_store.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| state/test_work_store_safety.py | GOOD | — | Behavior-focused test with scope matching directory intent. |

### tests/contract/

| File | Classification | Lane | Reason |
|------|----------------|------|--------|
| harness/test_launch_spec_parity.py | REWRITE | F | Monkeypatches private `_APPROVAL_POLICY_BY_MODE`; couples tests to adapter internals. |
| harness/test_spec_field_guards.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| harness/test_typed_contracts.py | GOOD | — | Behavior-focused test with scope matching directory intent. |

### tests/platform/

| File | Classification | Lane | Reason |
|------|----------------|------|--------|
| test_atomic.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| test_locking.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
| test_terminate.py | GOOD | — | Behavior-focused test with scope matching directory intent. |
