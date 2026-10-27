# lib/ops/ — Operation Policy Layer

Policy between surfaces (CLI, MCP) and mechanism: access control, input validation,
lifecycle sequencing. Calls `launch/`, `state/`, and (through launch) `harness/`.
Detail: `.context/CONTEXT.md`; the spawn subpackage: `spawn/AGENTS.md`.

## Rules

- **No mechanism in ops.** Building argv, merging envs, or projecting workspace roots
  belongs in `launch/`; harness-specific logic belongs in `harness/`.
- **`commands.py` is the operation manifest.** CLI and MCP discover operations there; an
  operation without an entry is invisible to both.
- **Resolve before persist.** Streaming-serve (`SpawnApplicationService.prepare_spawn()`)
  and the spawn subprocess prepare launch policy before creating the row. Keep
  resolution and policy checks on the preparation side of the row boundary, and the
  nested-spawn guardrail on the pre-materialization side (`.context/CONTEXT.md`,
  "Spawn preparation policy").
- **Work attachment is ops-layer.** `--work` resolves in
  `work_attachment.ensure_explicit_work_item()` before the spawn row exists.
  `resolve_active_work_id()` is the one read behind a "current" work item; commands
  that act on an item take one given, `current`, or nothing, so no caller carries an ID
  a `work switch` can invalidate. `attach_executions()` is the only place an
  execution's attachment changes (item links in the work store first, then rows and
  chat links in one session epoch, never nested with the work-store lock);
  `activate_work()` and `publish_work_event()` own the user-visible work events and
  paired telemetry. Details and the incident behind it: `.context/CONTEXT.md`.
- **Session references go through `ops/reference.py`** (`resolve_session_reference()`
  → `ResolvedSessionReference`) for `--continue`/`--fork` targets. `--from` resolves
  once through `ops/spawn/context_ref.resolve_context_ref()`; `-f` files in
  `launch/reference.py`.
- **Choose spawn status by intent**: see `spawn/AGENTS.md` (stored vs derived).
- **Sync artifacts belong to `hooks/builtin/autosync_store`**; ops resolves which
  directories are sync roots and never builds `.meridian/autosync/` paths.
