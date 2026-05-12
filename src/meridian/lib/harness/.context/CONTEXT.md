# lib/harness/ — Context

## Architecture

### Translation Pipeline

Every spawn goes through four steps:

```
SpawnParams                          harness-agnostic inputs
  ↓ adapter.resolve_launch_spec()
HarnessLaunchSpec                    harness-specific typed struct
  ↓ project_<harness>_spec_to_cli_args()
list[str] command + env dict         ready to exec
  ↓ subprocess / connection.start()
Running process                      events flow through SpawnManager drain loop
```

`SpawnParams` is the universal input struct (`adapter.py`). Every adapter declares
which fields it consumes (`consumed_fields`) and which it deliberately ignores
(`explicitly_ignored_fields`). Their union must cover all `SpawnParams` fields, or
`_enforce_spawn_params_accounting()` raises `ImportError` on startup.

### Two Launch Paths

**Subprocess path** (`lib/launch/`): forks a one-shot process. stdout/stderr are
read back. Process exit signals completion. Used for non-streaming spawns.

**Connection path** (`lib/harness/connections/`): starts a long-lived process then
connects bidirectionally. Events flow through the `SpawnManager` drain loop. Used
for streaming app server and `meridian chat`.

Per-harness mapping:
- Claude subprocess: `claude -p --output-format stream-json --verbose -`
- Claude connection: `claude -p --input-format stream-json --output-format stream-json --verbose` (stdin/stdout NDJSON, not WebSocket despite `claude_ws.py` name)
- Codex subprocess: `codex exec --json`; connection: `codex app-server` (real WebSocket, JSON-RPC 2.0)
- OpenCode subprocess: `opencode run`; connection: `opencode serve` (HTTP+SSE)

### Bootstrap Sequence

`__init__.py:_run_bootstrap()` runs exactly once on package import. Import order
is load-bearing:
1. Adapter modules register `HarnessBundle` entries as side effects.
2. Projection modules execute import-time drift guards.
3. Extractor modules bind `Protocol` implementations.
4. `_enforce_spawn_params_accounting()` validates all adapters account for all fields.

Do not import individual adapter modules before `ensure_bootstrap()` completes —
the accounting guard will run on a partial registration set and fail.

## Contracts

### SpawnParams Accounting Invariant

Every `SpawnParams` field must appear in each adapter's `consumed_fields` **or**
`explicitly_ignored_fields`. Enforced at import time by `launch_spec.py:_enforce_spawn_params_accounting()`. Adding a field to `SpawnParams` without updating all adapters → `ImportError` on startup. This is not documentation — it is enforcement.

Currently ignored fields:
- Claude ignores `report_output_path`
- Codex ignores `skills`, `agent`

### Projection Drift Guard

Each `projections/project_<harness>_*.py` module declares `_PROJECTED_FIELDS` and
`_DELEGATED_FIELDS`. `check_projection_drift()` runs at module load. If any spec
field is missing from both sets, it raises `ImportError`. Adding a field to a
harness-specific `LaunchSpec` without updating the corresponding projection module →
startup failure.

### `observe_session_id()` Priority Chain

Called exactly once per launch by the driving adapter after the executor returns.
Must not mutate adapter-instance state:

1. `connection_session_id` — live session ID from transport layer (present for connection-based paths)
2. `extract_session_id()` — extraction from spawn artifacts (`session_id.txt`, then JSONL history)
3. `current_session_id` — previously known ID, returned as fallback
4. `detect_primary_session_id()` — filesystem scan (only when `project_root` and `started_at_epoch` provided)

Callers treat the result as authoritative. Only skip an earlier step if its source is absent (no connection, no artifacts file).

### `HarnessContract` as Inspectable Surface

Every adapter declares a `HarnessContract` — a frozen Pydantic model that makes
capabilities explicit and machine-readable. Call `registry.get_contract(harness_id)`
to inspect without instantiating an adapter. The contract sub-models:

- `HarnessCapabilities` — boolean feature flags
- `ProjectionContract` — how content reaches the harness (which mode, who owns policy)
- `ExtractionContract` — what the adapter extracts from artifacts
- `ApprovalContract` — runtime HITL mode and permission routing
- `BootstrapContract` — subprocess-only vs managed-primary-attach, fork materialization
- `TransportContract` — transport IDs and whether observer/controller is required

### Terminal Event Classification

`semantics.py:terminal_outcome(event)` drives the drain loop's break condition.
Returns `TerminalEventOutcome(status, exit_code, error)` or `None`.
`event_type` is NOT globally unique — always qualified by `event.harness_id`.

Key mappings:
- Claude: `result` event with `is_error=True` → failed; `subtype in ("", "success")` and `terminal_reason in ("", "completed")` → succeeded
- Codex: `turn/completed` → succeeded; `error/connectionClosed` → failed
- OpenCode: `session.idle` → succeeded; `session.error` → failed

## Rationale

### Claude: PTY Capture for Primary Session ID

Claude's TUI emits its session ID to stdout only when stdout is a TTY. Meridian
uses `pty.openpty()` (POSIX) to observe this output without the harness knowing
it's captured. This is the minimum-intrusive mechanism for an otherwise unobservable
value. Windows does not support PTY — Claude primary uses a fallback detection path
(`session_detection.py`) on Windows.

### Claude: System-Prompt File Channel

`--append-system-prompt-file <path>` avoids `ARG_MAX` limits when skills and profile
bodies are large. The adapter writes content to a temp file in the spawn log dir.
This is not an optimization — it is required for spawns with many large skills.

### Content Projection: Inline vs File Channel

Claude has a separate system-prompt channel; Codex and OpenCode do not. All three
use `"inline"` routing for references (rendered via `render_reference_blocks()`),
with `"omitted"` for empty-body files. `supports_native_file_injection=False` for
all current harnesses — `--file` flags are not used.

### Codex: Managed-Primary Approval Routing

In managed-primary mode, Meridian is the first WebSocket client (turn owner).
`requestApproval` and `requestUserInput` are routed to the turn owner by the app
server — the TUI (secondary observer) can only observe notifications.
`CodexConnection._handle_server_request()` dispatches these through an injected
request handler:
- Spawn paths: `AutoAcceptHandler` (auto-approves all)
- Managed-primary attach: `InteractiveHandler` (surfaces as durable events)

### OpenCode: Workspace Env Merging

OpenCode workspace projection goes through `OPENCODE_CONFIG_CONTENT` (JSON env
override). If the parent process already exported this var, Meridian deep-merges
new workspace entries into the parent config rather than suppressing projection.
Suppression would silently drop workspace roots inherited from the spawner.

The implementation (`project_workspace_roots()`, `OPENCODE_CONFIG_CONTENT_ENV`)
lives in `launch/workspace_projection.py`, not in `harness/`. It was moved there
to break a circular import in Python 3.14: `harness/__init__` → `opencode.py` →
`opencode_http.py` → `workspace_projection` as a harness submodule, while harness
was still initializing. The module only depends on `core.types` — it never depended
on harness internals. `opencode_http.py` now imports `OPENCODE_CONFIG_CONTENT_ENV`
from `meridian.lib.launch.workspace_projection`.

## Patterns

### Adding a Harness

Touch every file in `HARNESS_EXTENSION_TOUCHPOINTS` (listed in `__init__.py`):
1. `core/types.py` — register `HarnessId`/`TransportId`
2. `<new_harness>.py` — adapter with `HarnessContract`, bundle registration, transport map side effect
3. `__init__.py:_run_bootstrap()` — add import wiring
4. `projections/project_<new_harness>_subprocess.py` + `_streaming.py`
5. `extractors/<new_harness>.py`
6. `registry.py:HarnessRegistry.with_defaults()`
7. `launch_spec.py:_enforce_spawn_params_accounting()` — update handled fields
8. `connections/<new_harness>_<transport>.py`
9. `projections/permission_flags.py`
10. `semantics.py:terminal_outcome()`

Missing any of these causes `ImportError` or `ValueError` at startup — the drift
guards make omissions loud.

### Anti-Patterns

**Don't access artifact files directly** — use the `ArtifactStore` protocol.
Direct path access bypasses the abstraction and breaks tests that mock artifacts.

**Don't assume `event_type` is globally unique** — always check `event.harness_id`
first. `turn/completed` is a Codex event; OpenCode uses `session.idle` for the same
semantic.

**Don't call adapter methods before `ensure_bootstrap()`** — the SpawnParams
accounting guard runs on a partial adapter set and will raise false `ImportError`.

**Don't skip `consumed_fields` / `explicitly_ignored_fields` declarations** — the
accounting invariant treats any uncovered field as a bug, not a warning.

## Related KB

> KB lives at `$MERIDIAN_CONTEXT_KB_DIR` (see `meridian context kb`).

- `$MERIDIAN_CONTEXT_KB_DIR/codebase/harness-adapters.md` — capability matrix, per-adapter flag projection, connections subpackage
- `$MERIDIAN_CONTEXT_KB_DIR/codebase/harness/overview.md` — translation pipeline, SpawnParams field table, base commands
- `$MERIDIAN_CONTEXT_KB_DIR/codebase/harness/event-semantics.md` — full terminal event table, activity transitions, drain policy integration
- `$MERIDIAN_CONTEXT_KB_DIR/codebase/harness/claude.md` — Claude flags, PTY path, session detection
- `$MERIDIAN_CONTEXT_KB_DIR/codebase/harness/codex.md` — Codex JSON-RPC, managed-primary, approval routing detail
- `$MERIDIAN_CONTEXT_KB_DIR/codebase/harness/opencode.md` — OpenCode SSE, SQLite sessions, workspace env merging
- `$MERIDIAN_CONTEXT_KB_DIR/architecture/launch-system.md` — how adapters plug into `build_launch_context()`

## Related .context/

- [../../state/.context/CONTEXT.md](../../state/.context/CONTEXT.md) — artifact store that `SpawnExtractor` reads from; atomic write primitives
- [../../launch/.context/CONTEXT.md](../../launch/.context/CONTEXT.md) — composition seam, four driving adapters, prepare/bind split, invariants
