# Adding a New Harness to Meridian

This guide covers the full process of adding a new AI coding agent as a Meridian
harness — from initial probe to distribution. Uses the Pi integration as the
worked example, with specific notes on what differs per harness type.

> **Pi note (2026-05):** Meridian no longer ships or wraps a Pi runtime.
> The Pi harness launches installed `pi` directly (`MERIDIAN_PI_BINARY`
> override, then `pi` on `PATH`). Meridian builds only its managed Pi
> extension JS bundles — no compiled Pi binary, no Bun/Node runtime
> fallback, no `meridian-pi` console script.

**Audience:** developers integrating a new harness into Meridian. Assumes
familiarity with the Meridian architecture (`AGENTS.md`, `src/meridian/AGENTS.md`,
`src/meridian/lib/harness/AGENTS.md`).

## Table of Contents

1. [Architecture Recap](#architecture-recap)
2. [Phase 0: Probe the Harness](#phase-0-probe-the-harness)
3. [Phase 1: Minimum Viable Spawn](#phase-1-minimum-viable-spawn)
4. [Phase 2: Runtime Resolution and Extension Build](#phase-2-runtime-resolution-and-extension-build)
5. [Phase 3: Session and History Parity](#phase-3-session-and-history-parity)
6. [Phase 4: Model, Catalog, and Mars Integration](#phase-4-model-catalog-and-mars-integration)
7. [Verification Checklist](#verification-checklist)
8. [Pi-Specific Current Status and Remaining Gaps](#pi-specific-current-status-and-remaining-gaps)

## Architecture Recap

Every spawn goes through a four-step translation pipeline:

```
SpawnParams                          harness-agnostic inputs
  ↓ adapter.resolve_launch_spec()
HarnessLaunchSpec                    harness-specific typed struct
  ↓ project_<harness>_spec_to_cli_args()
list[str] + env dict                 ready to exec
  ↓ subprocess / connection.start()
Running process                      events → SpawnManager drain loop
```

The nine touchpoints that must be modified for any new harness are listed in
`HARNESS_EXTENSION_TOUCHPOINTS` at `src/meridian/lib/harness/__init__.py:14`.
Missing any causes `ImportError` at startup — the drift guards are enforcement,
not documentation.

Before diving into code, read these orientation files:

- `src/meridian/lib/harness/AGENTS.md` — harness layer mental model and invariants
- `src/meridian/lib/harness/.context/CONTEXT.md` — full contracts, bootstrap sequence,
  session-ID chain
- `src/meridian/lib/harness/adapter.py` — `SpawnParams`, `HarnessContract`, and
  sub-model definitions (source of truth for what an adapter must implement)
- `src/meridian/lib/launch/AGENTS.md` — composition seam and finalization ownership

KB references for deeper context:

- `$MERIDIAN_CONTEXT_KB_DIR/codebase/harness-adapters.md` — capability matrix
- `$MERIDIAN_CONTEXT_KB_DIR/lessons/harness-integration.md` — lessons from
  previous integrations (PTY, managed-primary, semantic IR pattern)

## Phase 0: Probe the Harness

Before writing a single line of Meridian code, run the harness binary and
document its actual behavior. Design docs get things wrong — the probe catches
flag name mismatches, missing events, and assumed capabilities before they
become code.

### What to Probe

| Area | Questions | Pi Example |
|---|---|---|
| **CLI flags** | What are the exact flag names? What formats does the harness accept for model, output, session resume, system prompt? | `--mode json` (not `--output-format json` as assumed). `--append-system-prompt <text>` inline (not a file path). |
| **Event schema** | What JSON/JSONL events does the harness emit? What is the terminal event? Are there error events? | `agent_end` is the terminal event (not `turn.completed`). No `session.error` — errors surface via `stopReason:"error"`. |
| **Session ID** | How is the session ID emitted? Is it in stdout, a file, or both? | First JSONL line: `{"type":"session","id":"..."}`. Also in session files under `sessions/<cwd-escaped>/`. |
| **Fork/resume** | Can sessions be resumed? Forked? What are the flags? | `--session <id>` for resume, `--fork <id>` for fork. Native support confirmed. |
| **Permission model** | CLI permission flags? Approval bypass? | No CLI permission flags — permissions via extension event hooks. |
| **Config isolation** | Can session/config paths be isolated via env var? | `PI_CODING_AGENT_SESSION_DIR` scopes Pi session storage for Meridian-managed runs. |
| **Provider list** | How many providers? Does the list evolve with releases? | 20+ providers, upstream-owned. Don't hardcode — pass `--model` through. |
| **Skills** | Does the harness have a native skills/concepts system? How does it load them? | Pi implements Agent Skills standard (`--skill`). Deferred Meridian skill projection. |
| **System prompt** | How is system-level instruction delivered? | `--append-system-prompt <text>` inline. Differs from Claude (temp file path). |
| **Ambient discovery** | What ambient config/files does the harness discover at startup? | Extensions, skills, context files, prompt templates — all suppressed with `--no-*` flags for spawns. |

### Probe Methodology

1. Launch the harness binary directly (not through Meridian) with `--help` to
   discover flags.
2. Run a trivial prompt (`"Reply with exactly OK"`) in the intended spawn mode
   and capture stdout/stderr.
3. Examine the harness's session storage format (JSONL files, SQLite, etc.).
4. If the harness has an RPC/streaming mode, test that too even if Phase 1
   doesn't use it — you need to know the event schema for semantics.
5. Cross-reference every design assumption against the probe output. Fix the
   design before writing code.

**Pi probe result**: The original design had the output flag wrong (`--output-format json`
vs actual `--mode json`), the terminal event wrong (`turn.completed` vs `agent_end`),
and assumed session fork was unsupported when it was native. Catching these in the
probe saved multiple rounds of bug-fix commits.

## Phase 1: Minimum Viable Spawn

The goal: `meridian pi -m <model> "prompt"` launches a subprocess, Meridian
captures the output, extracts the report/usage/session-id, and surfaces the
result. No streaming, no interactive primary, no RPC.

### 1.1 Harness Identity and CLI Routing

**File: `src/meridian/lib/core/types.py`**

Add the harness ID:

```python
class HarnessId(str, Enum):
    CLAUDE = "claude"
    CODEX = "codex"
    OPENCODE = "opencode"
    PI = "pi"  # add here
```

No new `TransportId` is needed for subprocess-only harnesses — use the existing
`TransportId.STREAMING`. If the harness has a unique transport (e.g. WebSocket
like Codex), add a new `TransportId`.

**File: `src/meridian/cli/bootstrap.py`**

Add to the shortcut set:

```python
HARNESS_SHORTCUT_NAMES = frozenset({"claude", "codex", "opencode", "pi"})
```

This gives `meridian pi "task"` as a shorthand for `meridian --harness pi "task"`.

**File: `src/meridian/lib/launch/constants.py`**

Define base commands:

```python
BASE_COMMAND_PI_SUBPROCESS: Final = ("pi", "--mode", "rpc")
PRIMARY_BASE_COMMAND_PI:    Final = ("pi",)
```

`--mode rpc` enables the JSONL-over-stdio RPC transport for spawned sessions.
These constants are shared between the adapter, projection, and connection.

### 1.2 Adapter

**File: `src/meridian/lib/harness/pi.py`**

Create a class extending `BaseHarnessAdapter[ResolvedLaunchSpec]`:

```python
class PiAdapter(BaseHarnessAdapter[ResolvedLaunchSpec]):
    BASE_COMMAND = BASE_COMMAND_PI_SUBPROCESS
    PRIMARY_BASE_COMMAND = PRIMARY_BASE_COMMAND_PI
```

Every adapter must implement:

| Method/Property | Purpose |
|---|---|
| `id` | Return `HarnessId.PI` |
| `contract` | Declare `HarnessContract` with all sub-contracts |
| `capabilities` | Boolean feature flags (`supports_stream_events`, etc.) |
| `consumed_fields` / `explicitly_ignored_fields` | `SpawnParams` field accounting |
| `resolve_launch_spec()` | Map `SpawnParams` → `HarnessLaunchSpec` |
| `build_command()` | Produce the final argv list |
| `project_content()` | Map `ComposedLaunchContent` → harness channels |
| `env_overrides()` | Return child process env overrides |
| `extract_usage()`, `extract_session_id()`, `extract_report()` | Delegate to extractor |

**SpawnParams accounting**: Every field in `SpawnParams` must appear in
`consumed_fields` **or** `explicitly_ignored_fields`. The merge of both sets
for each adapter is enforced at import time by
`_enforce_spawn_params_accounting()`. Adding a `SpawnParams` field without
updating all adapters → `ImportError` on startup.

```python
_CONSUMED_FIELDS = frozenset({
    "prompt", "model", "effort", "extra_args", "control_root",
    "interactive", "continue_harness_session_id", "continue_fork",
    "appended_system_prompt", "user_turn_content", "mcp_tools",
    "projected_roots",
})
_EXPLICITLY_IGNORED_FIELDS = frozenset({
    "skills", "agent", "adhoc_agent_payload",
    "context_from_payload", "reference_items", "task_cwd",
})
```

**Capability decisions**:

- `supports_stream_events`: `True` if the harness emits structured JSONL events
- `supports_session_fork`: Probe this — Pi confirmed `True` with `--fork`
- `supports_native_skills`: Set `False` if Meridian skill projection is deferred
  (Pi has native skills but Meridian doesn't project them yet)
- `supports_native_agents`: `False` for harnesses without a native agent concept
  (agent body goes via `--append-system-prompt`)
- `supports_native_file_injection`: `False` for all current harnesses — reference
  content goes inline in the user turn
- `terminal_surface_modes`: include the surfaces the harness actually uses.
  Pi advertises primary interactive surfaces, preferring `PTY_MEDIATED` with
  `NATIVE_INHERIT` fallback.

**Bootstrap mode**: Phase 1 uses `BootstrapMode.SUBPROCESS_ONLY`. Upgrade to
`BootstrapMode.MANAGED_PRIMARY_ATTACH` only when adding interactive primary
session support (Phase 3+).

**Fork materialization**: If the harness natively supports fork (like Pi's
`--fork`), use `ForkMaterializationMode.NATIVE_CONTINUE_FORK`. If the harness
only supports resume, Meridian materializes forks by copying the session file
(`ForkMaterializationMode.MERIDIAN_MATERIALIZED_FORK`).

**Content projection** (`project_content()`): Maps the harness-agnostic
`ComposedLaunchContent` (system instruction, task context, user prompt) to
harness-specific channels. For Pi:
- System instruction → `--append-system-prompt` (inline, not a file path)
- Task context and user prompt → inline in the user turn

```python
def project_content(self, content: ComposedLaunchContent) -> ProjectedContent:
    system_prompt = render_system_instruction_blocks(content)
    task_context = render_task_context(content.reference_items, ...)
    user_turn = join_content_blocks(task_context, content.user_task_prompt)
    return ProjectedContent(
        system_prompt=system_prompt,
        user_turn_content=user_turn,
        channels=ProjectionChannels(
            system_instruction="system-field",
            user_task_prompt="user-turn",
            task_context="user-turn",
        ),
    )
```

**Env overrides**: Set harness-specific env vars for config isolation:

```python
def env_overrides(self, config: PermissionConfig) -> dict[str, str]:
    return {
        "PI_CODING_AGENT_SESSION_DIR": str(get_user_home() / "meridian-pi" / "sessions"),
    }
```

Pi uses `PI_CODING_AGENT_SESSION_DIR` to scope session storage to Meridian-managed
paths. Claude uses `CLAUDE_CONFIG_DIR`. Codex uses `CODEX_HOME`. Every harness
needs an explicit isolation contract — find the right env var(s) and set them.

**Bundle registration**: Register as a module-load side effect:

```python
register_harness_bundle(
    HarnessBundle(
        harness_id=HarnessId.PI,
        adapter=PiAdapter(),
        spec_cls=ResolvedLaunchSpec,
        extractor=PI_EXTRACTOR,
        connections={TransportId.STREAMING: PiConnection},
        projections=HarnessProjectionPorts(
            subprocess_cli_args=project_pi_spec_to_cli_args,
        ),
    )
)
```

### 1.3 Projection (RPC + Native TUI)

**Files:**
- `src/meridian/lib/harness/projections/project_pi_rpc.py`
- `src/meridian/lib/harness/projections/project_pi_native_tui.py`

Each projector maps `spec` → `argv_additions`. Declare `_PROJECTED_FIELDS`
(fields this projector handles) and `_DELEGATED_FIELDS` (fields owned by the
caller). The drift guard `check_projection_drift()` runs at import time.

**Command construction**:

```python
def project_pi_spec_to_cli_args(spec, *, base_command) -> list[str]:
    command = list(base_command)

    # Model and separate thinking level
    if spec.model:
        command.extend(("--model", spec.model))
    if spec.effort:
        thinking = _EFFORT_TO_THINKING.get(spec.effort)
        if thinking and not _has_model_override_in_passthrough(spec.extra_args):
            command.extend(("--thinking", thinking))

    # System prompt (inline, not file path — differs from Claude)
    if spec.appended_system_prompt:
        command.extend(("--append-system-prompt", spec.appended_system_prompt))

    # Session resume/fork
    if spec.continue_session_id:
        if spec.continue_fork:
            command.extend(("--fork", spec.continue_session_id))
        else:
            command.extend(("--session", spec.continue_session_id))

    # Meridian-managed Pi session directory
    command.extend(("--session-dir", "<managed-session-dir>"))

    # Suppress ambient discovery
    command.extend(("--no-extensions", "--no-skills",
                     "--no-context-files", "--no-prompt-templates"))

    # Managed Pi extensions (RPC projection)
    command.extend(("-e", "<managed-bash.js>", "-e", "<lifecycle.js>"))

    # Permission flags
    command.extend(resolve_permission_flags(spec.permission_resolver, HarnessId.PI))

    # User extra_args (passthrough, last-wins semantics for collisions)
    command.extend(spec.extra_args)

    return command
```

**Key projection decisions**:

- **System prompt channel**: Pi uses inline `--append-system-prompt <text>`.
  Claude uses `--append-system-prompt-file <path>` (temp file, avoids `ARG_MAX`).
  Codex and OpenCode inline everything. This is a per-harness decision — know
  the harness's channel before implementing.
- **Model format**: Pi expects `provider/model-id` (same as OpenCode). Claude
  uses `--model` with the full model string. Codex uses `--model` with the
  anthropic model ID. Pass through whatever the harness expects.
- **Effort → thinking level**: Pi accepts a separate `--thinking <level>` flag.
  Map Meridian effort values to harness-specific thinking levels. When the
  user's `extra_args` include a `--model`/`-m` override, suppress the managed
  `--thinking` flag so the user's model selection wins cleanly (same contract as
  when effort was encoded in the model token).
- **Isolation flags**: Every harness has ambient discovery (extensions, MCP
  servers, context files, project config). For spawns, suppress all of it.
  For Pi: `--no-extensions`, `--no-skills`, `--no-context-files`,
  `--no-prompt-templates`. Find the equivalent flags for your harness.
- **Passthrough collision**: When the user's `extra_args` contain a flag that
  the projector also manages, the user value wins (last-wins semantics).
  Log collisions at debug level so they're visible during integration testing.
- **Projection split**: RPC and native-primary projections are separate files.
  `project_pi_rpc.py` owns spawned RPC (`--mode rpc`, `--session-dir`, managed
  extensions). `project_pi_native_tui.py` owns primary launches (no `--mode`,
  no managed extension flags).

### 1.4 Extraction

**File: `src/meridian/lib/harness/extractors/pi.py`**

Implement `HarnessExtractor` with three core methods and one detection method:

```python
class PiHarnessExtractor(HarnessExtractor[ResolvedLaunchSpec]):
    def extract_session_id(self, artifacts, spawn_id) -> str | None: ...
    def extract_usage(self, artifacts, spawn_id) -> TokenUsage: ...
    def extract_report(self, artifacts, spawn_id) -> str | None: ...
    def detect_session_id_from_event(self, event) -> str | None: ...
    def detect_session_id_from_artifacts(self, *, spec, launch_env,
                                          child_cwd, runtime_root) -> str | None: ...
```

**Session ID**: Pi emits `{"type":"session","id":"..."}` as the first JSONL line.
Parse from artifact output. Also scan the harness's session directory
(`sessions/<cwd-escaped>/*.jsonl`) as a fallback for when stdout capture fails.

**Usage**: Pi puts usage in the last `message_end` with
`message.role=="assistant"`. Extract `message.usage.input`, `message.usage.output`,
`message.usage.cacheRead`, `message.usage.cacheWrite`. Cost in USD is available
at `message.usage.cost.total` if the provider reports it.

**Report**: Pi's final assistant text is in `agent_end.messages[-1].content`
(where `role=="assistant"` and `content[].type=="text"`). Walk backwards through
the event list and extract the last assistant text.

**Fallback session ID from files**: If `--continue` was used (resume, not fork),
the session ID was already known. For fresh spawns where stdout capture fails,
scan `$PI_CODING_AGENT_SESSION_DIR/<cwd-escaped>/` for the most recently
modified JSONL file and read its session header. CWD escaping: slashes `/`
become `--`, directory name ends with `--`.

### 1.5 Connection/Streaming Runner

**File: `src/meridian/lib/harness/connections/pi_rpc.py`**

Even in Phase 1 subprocess-only mode, implement a `HarnessConnection` subclass
that drains JSONL events through Meridian's streaming runner. The SpawnManager
drain loop calls `terminal_outcome(event)` on each event; when that returns
non-None, the drain breaks.

```python
class PiConnection(HarnessConnection[ResolvedLaunchSpec]):
    async def start(self, config, spec): ...
    async def stop(self): ...
    async def events(self) -> AsyncIterator[HarnessEvent]: ...
    async def send_cancel(self): ...        # Signal the process
    async def send_user_message(self): ...  # Raise ConnectionNotReady in Phase 1
```

**Event stream reading**: Read JSONL lines from `process.stdout`,
parse each as JSON, extract `type` field, emit `HarnessEvent(event_type, payload, harness_id)`.

**stderr handling**: Redirect `stderr` to `<spawn_dir>/stderr.log`. Never parse stderr
for structured data — it's diagnostic only.

**cwd**: Launch in `config.task_cwd` (the agent's working directory) when provided,
falling back to `config.control_root`.

**Process cancellation**: On `send_cancel()`, send SIGINT. Wait up to 5 seconds
for graceful shutdown, then SIGKILL/`process.kill()`.

**Env propagation**: Inherit parent env, apply adapter env overrides, block
`MERIDIAN_ACTIVE_WORK_ID` and `MERIDIAN_ACTIVE_WORK_DIR` from child scope.

### 1.6 Event Semantics

**File: `src/meridian/lib/harness/semantics.py`**

Three functions must handle the new harness:

**`terminal_outcome(event)`** — classifies whether an event completes a spawn drain:

```python
if event.harness_id == HarnessId.PI.value and event.event_type == "agent_end":
    messages = event.payload.get("messages", [])
    last_assistant = next(
        (m for m in reversed(messages) if m.get("role") == "assistant"), None
    )
    if last_assistant and last_assistant.get("stopReason") == "error":
        return TerminalEventOutcome(status="failed", exit_code=1, error="pi_stop_error")
    return TerminalEventOutcome(status="succeeded", exit_code=0)
```

**`activity_transition(event)`** — maps events to `"turn_active"` or `"idle"`:

```python
if event.harness_id == HarnessId.PI.value:
    if event.event_type in {"agent_start", "turn_start", "message_start",
                             "message_update", "tool_execution_start",
                             "tool_execution_update"}:
        return "turn_active"
    if event.event_type in {"turn_end", "agent_end"}:
        return "idle"
```

**`clears_signal(event)`** — which event clears a pending user signal:

```python
if event.harness_id == HarnessId.PI.value:
    return event.event_type == "agent_end"
```

**Golden rule of semantics**: `event_type` is NOT globally unique. Always qualify
by `event.harness_id` first. `turn/completed` is Codex; OpenCode uses
`session.idle` for the same semantic.

If the harness can multiplex child work on the same stream, also expose
`HarnessConnection.primary_event_scope`. The scope identifies the parent conversation
whose terminal events may complete the spawn. Existing examples:

- Codex uses the main turn `threadId`; subagent-thread `turn/completed` events stay in
  history but do not complete the parent.
- OpenCode uses the launched parent `sessionID`; child task `session.idle` /
  `session.error` events stay in history but do not complete/fail the parent, clear
  parent signals, or supply the parent report.

Only add scope filtering where the harness stream truly mixes parent and child
activity. The drain loop must still persist child events before classification so
`meridian session log` remains complete.

### 1.7 Permission Flag Projection

**File: `src/meridian/lib/harness/projections/permission_flags.py`**

Return the CLI permission/approval flags for the harness:

```python
if harness_id == HarnessId.PI:
    return ()  # Pi: permissions via extension event hooks, not CLI flags
```

Claude returns `--dangerously-skip-permissions` when appropriate. Codex returns
flags from its own permission model. Map the `PermissionResolver` config to
whatever the harness expects.

### 1.8 Registry

**File: `src/meridian/lib/harness/registry.py`**

Register in `with_defaults()`:

```python
registry.register(PiAdapter())
```

### 1.9 Bootstrap Import Wiring

**File: `src/meridian/lib/harness/__init__.py`**

Add imports to `_run_bootstrap()`:

```python
from meridian.lib.harness import pi as _pi
from meridian.lib.harness.projections import project_pi_rpc as _project_pi_rpc
from meridian.lib.harness.projections import project_pi_native_tui as _project_pi_native_tui
from meridian.lib.harness.extractors import pi as _pi_extractor
```

Import order is load-bearing: adapters first (bundle side effects), then
projections (drift guards), then extractors, then accounting enforcement.
Do not import individual adapter modules before `ensure_bootstrap()` completes.

### 1.10 Launch Spec Accounting

**File: `src/meridian/lib/harness/launch_spec.py`**

The `_enforce_spawn_params_accounting()` function runs after all adapters are
registered. It checks that for each adapter, every `SpawnParams` field appears
in `consumed_fields | explicitly_ignored_fields`. If a field is added to
`SpawnParams` without updating the adapter, startup raises `ImportError`.

This is automatic — the new adapter's fields are checked like any other.
Just make sure both sets are complete.

## Phase 2: Runtime Resolution and Extension Build

Not every harness needs a wrapper binary. Pi is an installed native binary
(`pi` on `PATH`), so there is no runtime to bundle or compile. Meridian only
needs to resolve the installed Pi and build its managed extensions.

### 2.1 Runtime Resolution

`PiAdapter` resolves the Pi binary with this precedence:

1. `MERIDIAN_PI_BINARY=/path/to/pi` override.
2. `pi` from `PATH` (via `shutil.which`).
3. Failure with install guidance.

No bundled runtime fallback. No Node or Bun fallback. No `meridian-pi` console
script.

Required failure messages:

- Missing runtime:
  ```
  Pi is not installed or not on PATH.
  Install Pi using the official Pi instructions, then run `pi --version` and retry.
  Set MERIDIAN_PI_BINARY=/path/to/pi to use a non-PATH installation.
  ```
- Incompatible runtime:
  ```
  Installed Pi at <path> is not compatible with Meridian's Pi harness: <probe failure>.
  Run `pi update`, or set MERIDIAN_PI_BINARY=/path/to/pi to another compatible Pi binary.
  ```

Configuration/session isolation is done through env vars passed to the child
process (`PI_CODING_AGENT_SESSION_DIR`) — no wrapper binary needed.

### 2.2 Compatibility Probe

After resolving the binary, `PiAdapter` runs a bounded, side-effect-light probe:

- `pi --version` exits 0.
- `pi --help` exits 0 and advertises required surfaces:
  - `--mode` / `rpc` for spawned RPC;
  - `--model` for model projection;
  - `--append-system-prompt`, `--session`, and `--fork` for projected spawned controls;
  - `--session-dir` or supported session-dir env behavior;
  - extension flags (`--no-extensions`, `-e`/`--extension`) for managed spawned sessions.

Primary can tolerate lack of RPC-specific flags only if the user is not launching
spawned Pi; spawned Pi must fail before process launch if RPC/extension surface
is missing.

The probe result (path, version) is stored in spawn/primary metadata for
observability.

### 2.3 Primary Launch (Native TUI)

```text
<resolved-pi> [--model <model>] [--thinking <level>] [--append-system-prompt <text>] [--session <id> | --fork <id>] [permission flags] [extra_args...]
```

Environment:

```text
_MERIDIAN_PI_SESSION_ROLE=primary
MERIDIAN_SPAWN_ID=<primary-spawn-id>
PI_CODING_AGENT_SESSION_DIR=<user_home>/meridian-pi/sessions
```

Primary must not pass `--mode rpc`, managed extensions, or any wrapper-only flags.

### 2.4 Spawned RPC

```text
<resolved-pi> --mode rpc --model <model> [--thinking <level>] --session-dir <dir> --no-extensions -e <managed-bash.js> -e <lifecycle.js> [session flags] [permission flags]
```

Environment:

```text
_MERIDIAN_PI_SESSION_ROLE=spawned
MERIDIAN_SPAWN_ID=<spawn-id>
PI_CODING_AGENT_SESSION_DIR=<spawn/session scoped dir>
```

Spawned prompt delivery is prompt-first over `PiRpcConnection`; no initial prompt
is embedded in the CLI tail.

### 2.5 Extension Build

Meridian still ships distributable JS for managed Pi extensions. This is
separate from the Pi binary — Meridian owns the extension bundles, not Pi.

Implementation options, in preference order:

1. Ship committed compiled extension JS as package data, built during Meridian
   release/preflight.
2. Build extensions during package build from TypeScript sources.
3. Dev-only rebuild command for contributors.

Do not require Bun at runtime or build time. Use the existing Node/npm
toolchain already used by extension tests.

The runtime launch path must only need:

- Python Meridian package;
- installed compatible `pi`;
- extension JS files already present in the Meridian package.

Extension source lives under `src/meridian/pi_runtime/extensions/`:
- `managed-bash/` — managed bash, `/ps`, background bash records and task pings
- `meridian-spawn-watch/` — correlated spawn discovery, `/spawn`, wait/cancel/log actions

The compiled output is shipped as package data; the directory name
(`pi_extensions/` or `harness/pi/extensions/`) no longer implies a bundled
Pi runtime.

## Phase 3: Session and History Parity

### 3.1 Generic `history.jsonl` Event Persistence

Every spawn writes `history.jsonl` in the spawn log directory. This is the
generic event persistence layer — raw JSONL events from the harness, one
per line, with Meridian-added metadata. This works automatically for any
harness that uses the streaming runner drain loop.

### 3.2 Native Session File Resolution

Harnesses store their own session files independently. For Pi, session files
live under `$PI_CODING_AGENT_SESSION_DIR/<cwd-escaped>/<timestamp>_<uuid>.jsonl`.

The extractor needs to:
1. Know where the harness stores session files
2. Know the file naming convention
3. Know how to map the Meridian spawn's CWD to the harness's session directory

This is harness-specific and must be implemented in the extractor's
`detect_session_id_from_artifacts()` method. Pi's CWD encoding: slashes become
`--`, directory name ends with `--`.

### 3.3 Readable `meridian session log` Translation

`meridian session log` renders a human-readable transcript from `history.jsonl`.
This works through the `TranscriptProvider`/`TranscriptEventParser` system in
`src/meridian/lib/harness/transcript.py`.

Each harness needs:
1. A `TranscriptProvider` that knows how to iterate events from its native
   session files (or falls back to `history.jsonl`)
2. A `TranscriptEventParser` that extracts `TranscriptMessage(role, content)`
   from harness-specific event schemas

**Pi current status**: Spawned Pi RPC runs persist canonical `history.jsonl`, and
`meridian session log <pi-spawn-id>` renders readable transcript entries from Pi
`message_end` events: user prompts, assistant text, tool calls/results, and custom
follow-up pings. Native Pi session-file lookup remains best-effort metadata; the
spawn history is the session-log authority for Meridian-managed Pi RPC spawns.

### 3.4 Export/Search Implications

Session-log parity means the same transcript parser feeds log, export, and search
surfaces. For Pi RPC, keep `history.jsonl` event persistence and `message_end`
translation in sync; if Pi changes its event schema, update
`src/meridian/lib/harness/transcript.py` and the Pi transcript parser tests before
trusting search/export output.

## Phase 4: Model, Catalog, and Mars Integration

### 4.1 Model Aliases Are Harness-Specific

Do not assume that model aliases from one harness work with another. `sonnet`
maps to `anthropic/claude-sonnet-4` in Claude; it may map to
`anthropic/claude-sonnet-4-6` in Pi, or not be recognized at all.

Each harness has its own provider registry and model namespace. A model string
that works with `--harness claude` may need a different format for
`--harness pi`.

**Current Pi status**: Pi supports 20+ providers with model resolution built in.
The harness passes `--model <string>` through to Pi, which handles resolution.
However, **Mars model aliases do not yet include Pi-compatible model paths**.
Users must currently pass explicit Pi-compatible model strings
(e.g. `anthropic/claude-sonnet-4`).

### 4.2 What Mars Needs to Support a Harness

For Mars to fully support a harness, the following are needed:

1. **Model alias mapping**: Mars model entries need `harness_candidates` /
   `runnable_paths` entries for the new harness. Each entry maps a Meridian
   model alias (e.g. `sonnet`) to a harness-specific model ID string
   (e.g. `anthropic/claude-sonnet-4`).

2. **Provider discovery**: Mars needs to know which providers the harness
   supports and how to validate model strings.

3. **Catalog agent profiles**: Agent profiles should be able to specify
   `harness: pi` and `model: sonnet` and have resolution produce the correct
   Pi model string.

4. **`meridian mars models list --live`**: Should show which models are runnable
   through the Pi harness.

**Current Pi status**: None of the above is implemented. Pi-compatible model
aliases, provider discovery, and catalog integration are deferred. Users
must use explicit model strings with `--harness pi`.

### 4.3 Interim Workaround

Until Mars supports the harness:

```bash
# Use explicit provider/model-id strings
meridian pi -m anthropic/claude-sonnet-4 "task"
meridian pi -m openai/gpt-5.4-mini "task"

# Or discover available models from the harness directly
pi --list-models
```

## Verification Checklist

Run these in order. Each step gates the next.

### Unit Tests

- [ ] Adapter resolves `SpawnParams` correctly
- [ ] Projection maps all spec fields to CLI args
- [ ] Extraction parses session ID, usage, report from sample JSONL
- [ ] Semantics classify terminal/activity/signal events correctly
- [ ] CLI shortcut parsing (`meridian pi`) routes to the right harness
- [ ] Registry returns the adapter, contract, and extractor
- [ ] Drift guards pass (both SpawnParams accounting and projection field coverage)

```bash
uv run pytest tests/unit/harness/test_pi_projection.py
uv run pytest tests/unit/harness/test_pi_extractor.py
uv run pytest tests/unit/harness/test_pi_integration.py
uv run pytest tests/unit/cli/test_bootstrap_pi_shortcut.py
```

### Manual Pi smoke (real runtime)

Follow `tests/smoke/pi-manual.md` and `tests/smoke/pi-rpc-quiescence.md`. Requires real
`pi` on `PATH`, Node 24+ for extension builds, and auth under `~/.pi/agent`
(`PI_CODING_AGENT_DIR`).

- [ ] Happy spawn succeeds; `report.md` is a normal reply, not lifecycle JSON only
- [ ] Auth/model failures surface readable errors (#262), not `cleanup_completed` bodies
- [ ] `pi_paths`: Meridian bundles under `~/.meridian/pi/extensions/` (`-e` targets); `_MERIDIAN_PI_STATE_DIR` for extension runtime state

### Runtime Resolution Smoke

- [ ] Missing `pi` on `PATH` fails fast with install instructions
- [ ] Incompatible `pi` (missing `--mode rpc` in `--help`) fails with update instructions
- [ ] `MERIDIAN_PI_BINARY` may point at a **real** non-PATH `pi` install (not a stub script)

### Real Harness Spawn Smoke

- [ ] `meridian pi -m <cheap-model> "Reply with exactly OK"` exits 0
- [ ] `meridian spawn --harness pi -m <cheap-model> "Reply with exactly OK"`
  exits 0 and reports status `succeeded`
- [ ] `meridian spawn show <spawn-id> --verbose` shows report, usage, session ID
- [ ] Resume works: `meridian pi --continue <session-id> "continue"`
- [ ] Fork works: `meridian pi --fork <session-id> "fork"`

Use a cheap model (e.g. `gpt-5.4-mini`, `haiku`) for these — reserve
expensive models for reasoning-heavy tasks.

### Session Log / Transcript Smoke

- [ ] `meridian session log <pi-spawn-id>` produces readable output
- [ ] Native session files are discoverable from spawn metadata
- [ ] Export formats include Pi session content

**Pi status**: Spawned Pi RPC history renders readable transcript entries from
`message_end` events. Native session files may still exist, but Meridian-managed
spawn history is the authority for `session log`.

### Packaging / Wheel Smoke

- [ ] `uv build --no-sources` succeeds
- [ ] Installed wheel: `uv run --isolated --no-project --with <wheel> meridian --help`
  includes the Pi harness
- [ ] Extension JS files ship in the wheel package data
- [ ] `pyright` reports 0 errors
- [ ] `ruff check .` passes

### Pre-Push

```bash
uv run ruff check .
uv run --extra dev python -m pyright
uv run pytest -x -q
uv build --no-sources
```

All must pass. The pre-push hook enforces this automatically.

## Pi-Specific Current Status and Remaining Gaps

### Implemented (Phases 1-2)

| Area | Status | Notes |
|---|---|---|
| `HarnessId.PI` + CLI shortcut | Done | `meridian pi "task"` works |
| PiAdapter + bundle registration | Done | Full `HarnessContract`, SpawnParams accounting |
| Runtime resolution | Done | Resolves `pi` from `MERIDIAN_PI_BINARY` / `PATH`; no bundled fallback |
| Compatibility probe | Done | `pi --version` + `pi --help` surface check; fail-fast with install/update guidance |
| Subprocess projection | Done | `pi --mode rpc ...`, inline system prompt, isolation flags |
| Primary native TUI launch | Done | `pi [--model ...] [--session ...]`, no `--mode`, no extensions |
| PiConnection (JSONL drain) | Done | Streaming runner drain loop, session ID capture, stderr logging |
| PiExtractor | Done | Session ID, usage, report from artifacts + events |
| Event semantics | Done | `agent_end` terminal, activity transitions, signal clearing |
| Permission flags | Done | Empty tuple (Pi uses extension hooks) |
| Managed extension build | Done | Extension JS bundles ship as package data; dev-rebuild via Node/npm |
| Config isolation via env | Done | `PI_CODING_AGENT_SESSION_DIR` set by adapter, no wrapper needed |

### Remaining Gaps

| Gap | Severity | What's needed |
|---|---|---|
| **Native Pi session-file transcript provider** | Low | Spawned Pi RPC `history.jsonl` now renders readable `session log` output from `message_end` events. A native Pi session-file provider may still be useful for non-Meridian Pi sessions, but it is no longer required for Meridian-managed spawn observability. |
| **Mars model aliases/catalog** | Medium | Mars does not yet include Pi-compatible model paths in its alias resolution. Users must pass explicit `provider/model-id` strings. Need: `harness_candidates` / `runnable_paths` entries in Mars model definitions, provider discovery, and agent profile resolution for `harness: pi`. |
| **Web extensions/tools** | Deferred | Built-in `web_search` and `web_fetch` extensions. These are Pi-native extensions that need authoring and bundling. |
| **Notifications** | Deferred | Meridian spawn completion notifications surfaced through Pi's UI. |
| **`meridian doctor` integration** | Deferred | Health checks for Pi binary, version, extensions, provider availability. |

### Quickest Next Fixes

1. **Model aliases**: Add `harness_candidates` with Pi runnable paths to Mars
   model definitions for commonly used models.

## Reference: Pi Integration Files

### Created

```
src/meridian/lib/harness/pi.py                                          # Adapter + bundle
src/meridian/lib/harness/projections/project_pi_rpc.py                  # Spawned RPC CLI arg projection
src/meridian/lib/harness/projections/project_pi_native_tui.py           # Primary native TUI CLI arg projection
src/meridian/lib/harness/extractors/pi.py                               # Artifact extraction
src/meridian/lib/harness/connections/pi_rpc.py                          # JSONL event drain
src/meridian/pi_runtime/extensions/managed-bash/                        # Managed bash extension source
src/meridian/pi_runtime/extensions/meridian-spawn-watch/                # Spawn watch extension source
tests/unit/harness/test_pi_projection.py                                # Projection tests
tests/unit/harness/test_pi_extractor.py                                 # Extractor tests
tests/unit/harness/test_pi_integration.py                               # Integration tests
```

### Modified

```
src/meridian/lib/core/types.py                                          # + HarnessId.PI
src/meridian/cli/bootstrap.py                                           # + 'pi' shortcut
src/meridian/lib/launch/constants.py                                    # + PI base commands
src/meridian/lib/harness/__init__.py                                    # + pi bootstrap import
src/meridian/lib/harness/registry.py                                    # + PiAdapter registration
src/meridian/lib/harness/semantics.py                                   # + Pi event cases
src/meridian/lib/harness/projections/permission_flags.py                # + Pi empty tuple
src/meridian/lib/launch/launch_types.py                                 # + TerminalSurfaceMode
src/meridian/lib/harness/pi_runtime_resolver.py                         # + runtime resolution/probe
```

### Deleted

```
src/meridian/cli/pi_entrypoint.py                                       # Wrapper entrypoint (no longer needed)
src/meridian/pi_runtime/runner.mjs                                      # Node SDK runner (no longer needed)
src/meridian/pi_runtime/compile_runner.mjs                              # Bun compile entry (no longer needed)
tests/unit/cli/test_pi_entrypoint.py                                    # Wrapper tests (no longer needed)
```
