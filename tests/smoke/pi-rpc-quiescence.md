# qa-validated: pi-rpc-quiescence

# Pi RPC quiescence smoke and diagnostics (S1-S36)

Manual checks for Pi RPC quiescence. All scenarios use a **real** installed `pi`
binary—never a stub, shim, or fake runtime. Run the short gate in
[`pi-manual.md`](pi-manual.md) first.

The guide has two tiers:

- **Tier 1** is the merge gate for Pi-related changes.
- **Tier 2** preserves the full diagnostic surface. Run the relevant cluster when
  Tier 1 fails or when changing that plumbing.

## Prerequisites

```bash
. tests/smoke/scripts/pi-setup.sh --build-extensions
```

- **Node 24+** on `PATH` (extension build; matches CI)
- **Real `pi` on `PATH`**: `pi --version` succeeds and `pi --help` includes
  `--mode rpc`
- **Provider auth** under `~/.pi/agent`; Meridian passes that Pi-owned tree through
  `PI_CODING_AGENT_DIR`
- **Spawn sessions** under `~/.meridian/meridian-pi/sessions/<spawn-id>/` (or the
  equivalent `MERIDIAN_HOME` root)
- **Meridian extension bundles** under `~/.meridian/pi/extensions/` (or the
  in-tree `src/meridian/pi_runtime/dist/extensions/` during development); launches
  pass the selected stable bundle entrypoints to Pi with `-e`
- Optional `MERIDIAN_PI_BINARY=/path/to/real/pi` for a compatible non-`PATH`
  install only

Run spawned checks from this repository root so nested local-source commands use the
branch under test.

## Reading terminal state and phases

Pi spawned completion is descendant-quiescence driven and has **no default total
wall-clock ceiling**. A session may legitimately remain active through successive child
waves. `--timeout` (in minutes) or `MERIDIAN_TIMEOUT` is the opt-in, non-renewing
absolute ceiling for the whole attempt. Child-wave and notification deadlines are
separate, wave-local safety bounds.

Terminal state is published before best-effort descendant/process cleanup and
connection teardown. The completion state machine goes from publication to
`finalized`; there is no `cleaning` completion phase. Cleanup lifecycle events
(`cleanup_running`, `cleanup_completed`, `cleanup_escalated`, or `cleanup_failed`) are
post-publication diagnostics and may arrive later. They come from Pi's
`pi_drain_teardown.py`; `drain_teardown.py` is the harness-neutral teardown contract.

**Phase inspection rule:** after `spawn wait`, `state.json`, or another command first
shows a terminal outcome, poll `history.jsonl` with a bounded wait before asserting on
`finalized` or cleanup phases. Never require those phase rows to exist immediately.

---

# Tier 1 — must-run smoke

Run S1-S12 before merging a Pi-related change. S9 and S11 are fault contracts with
automated coverage; inspect them rather than corrupting a real Pi installation.

## S1: Basic Pi RPC spawn

```bash
uv run meridian spawn --harness pi -m <pi-model> -p "Reply OK and run no commands"
```

Expect a normal reply and terminal status `succeeded`, with no wait on nonexistent
child work. With the bounded phase wait above, history reaches `finalized`.

## S2: Managed bash blocking command

Prompt:

```text
Run `printf hello` with bash and report the output.
```

Expect `bash` to return `state: "exited"`, `exit_code: 0`, and output containing
`hello`. A short blocking command does not emit `meridian.subspawn.start`.

## S3: Bash-tool timeout becomes tracked work and drains

Prompt:

```text
Run `sleep 5 && echo done` with bash using timeout 1000ms, then report the job id.
```

Expect the tool call to return `state: "running"` and a `job_id`. History contains a
`meridian.subspawn.start` with `wait_policy: "tracked"`, then a matching
`meridian.subspawn.end`. This bash-tool timeout does **not** set Meridian's absolute
attempt timeout: the parent remains active until the tracked job and its continuation
drain, then succeeds.

## S4: Detached job does not block quiescence

Prompt:

```text
Start `sleep 30` in the background with wait_policy detached, then say DETACHED.
```

Expect the start event to use `wait_policy: "detached"` and the session to quiesce
without waiting 30 seconds. Detached work is user-owned and is not killed by Pi
quiescence cleanup.

## S5: RPC multi-turn manual injection

```bash
uv run meridian spawn --harness pi -m <pi-model> --bg -p "Reply FIRST and wait."
uv run meridian spawn inject <spawn-id> "Reply SECOND."
uv run meridian spawn wait <spawn-id>
```

Expect one spawned Pi process to handle both turns and complete after the second turn
reaches quiescence.

## S6: Tracked child completion wakes the parent

Prompt:

```text
Start `sleep 3 && echo child-done` as tracked background work. When it completes, summarize the result.
```

Expect the parent to idle while the child runs, then history to contain
`meridian.notification.queued` and `meridian.notification.delivered`. The disk-backed
bash record at `<runtime-root>/pi-bash/<spawn-id>/bash-records.json` moves through
start/end state, a follow-up turn reports the result, and completion occurs only after
that turn quiesces.

### S6b: Fast tracked completion still triggers a follow-up

Prompt:

```text
Start `sleep 0.1 && echo fast-done` as tracked background work. Continue immediately, then summarize when background work completes.
```

The child may start and end before the first `agent_end`. Even then, expect queued and
delivered notification events, a follow-up that reports `fast-done`, and completion
only after that follow-up.

### S6c: Nested stale detection is bounded and read-only

1. Start a spawned Pi RPC run with tracked work.
2. After its spawn row exists, force-kill the runner process.
3. From `MERIDIAN_DEPTH=1`, run `meridian spawn wait <spawn-id>`.

Expect stale shaping only after the startup grace (about 15 seconds) and recent-activity
grace (about 120 seconds across heartbeat/history/spawn/bash-record mtimes). Nested
wait/show may report `running` during those windows, but does not hang forever. It
eventually returns synthetic `failed` with `stale_nested_read` (or
`stale_nested_read_no_pid` without runner PID metadata), while the on-disk
`spawns/<spawn-id>/state.json` remains active because the nested read does not reconcile
or finalize it.

### S6d: Local-source nested Pi spawn follows persisted descendants

```bash
PROJECT=$PWD
MERIDIAN_HOME=$(mktemp -d) timeout 240s uv run meridian -C "$PROJECT" --harness pi spawn \
  -m gpt-5.4-mini \
  -p "Run exactly this command as a tracked child spawn and wait for it to finish: uv run meridian -C '$PROJECT' --harness pi spawn -m gpt-5.4-mini -p 'Reply exactly NESTED_CHILD_OK and run no commands.' --timeout 1 --format json. After the child completes, reply exactly PARENT_AFTER_CHILD_OK and include the child spawn id. Do not run any other commands." \
  --timeout 4 --format json
```

Expect both spawns to succeed with their requested markers. The child row has the
parent's ID as `parent_id` and an `originating_bash_id` beginning `b-` when managed
bash launched it. Parent completion waits for the reconciled transitive descendant
tree to become terminal. The `--timeout` values here are absolute attempt ceilings in
minutes, not child-wave deadlines.

## S7: Primary Pi uses the native TUI

```bash
uv run meridian --harness pi -m <pi-model>
```

Expect Pi's native TUI without `--mode rpc`. Meridian does not create a
`PiRpcConnection` or translate prompts through an `input()` loop. It records
best-effort wrapper metadata (primary spawn ID, PID, cwd, session directory, exit
status, and discovered Pi session ID when available), but applies no spawned RPC
quiescence stop to the primary session.

## S8: Tracked child failure notifies the parent

Prompt:

```text
Start `sh -c 'sleep 1; exit 7'` as tracked background work. Handle the result.
```

Expect the end event to show `success: false` and `exit_code: 7`, followed by a failure
notification turn. The final spawn outcome reflects what the parent does with that
failure; handling it can still produce a successful parent outcome.

## S9: Notification delivery failure fails instead of hanging

Do not inject shims into this real-runtime guide. The contract is covered by
`tests/integration/streaming/test_pi_quiescence.py` and
`tests/integration/streaming/test_pi_characterization.py`.

If a real delivery failure occurs, expect terminal `failed` after tracked descendants
drain, not a permanent pending-notification wait. History can contain
`meridian.notification.queued` followed by `meridian.notification.failed`.

## S10: Pi exits with tracked work pending

```bash
uv run meridian spawn --harness pi -m <pi-model> --bg -p "Start tracked background work: sleep 30."
# After meridian.subspawn.start appears:
kill <pi-process-pid>
uv run meridian spawn wait <spawn-id>
```

Expect terminal `failed` with `pi_process_exited_with_tracked_children`, rather than
success from an earlier `agent_end`. After publication, best-effort cleanup first
cancels persisted descendants and then terminates tracked process groups when PID/PGID
metadata exists. Detached work is not killed. Use the bounded phase wait if checking
cleanup telemetry.

## S11: Malformed coordination state fails closed

Do not corrupt a normal smoke environment. Malformed lifecycle parsing is covered by
`tests/unit/harness/test_pi_integration.py`; unreadable/private-disk evidence and
recovery behavior are covered under `tests/integration/streaming/`.

The contract is typed unknown evidence—not an empty/no-work snapshot. A completion
candidate waits for the single relevant completion deadline and fails explicitly if
the evidence remains unreadable.

## S12: Opt-in absolute attempt timeout

Use a deliberately long tracked job and a short **minutes-valued** attempt ceiling:

```bash
uv run meridian spawn --harness pi -m <pi-model> --timeout 0.05 \
  -p "Start tracked background work: sleep 9999."
```

Expect the session to remain active only until the roughly three-second absolute
ceiling, then terminal status `timed_out`. Terminal publication does not wait for
descendant/process cleanup. Cleanup continues asynchronously and best-effort; detached
jobs remain user-owned. Without `--timeout` or `MERIDIAN_TIMEOUT`, Pi has no equivalent
total wall-clock ceiling while legitimate descendant waves continue.

---

# Tier 2 — deep diagnostics appendix

Run the cluster that matches the failure or code being changed. Phase assertions still
follow the bounded-wait rule near the top of this guide.

## Runtime and phase diagnostics (S13-S19)

### S13: Pi need not emit a pre-prompt session event

```bash
(sleep 5) | timeout 3s pi --mode rpc --model <pi-model> --no-extensions --no-skills --no-context-files --no-prompt-templates
```

The probe may produce no stdout before timeout. Meridian must therefore send the first
prompt without waiting for a `session` event.

### S14: Prompt response failure does not hang

```bash
uv run meridian spawn --harness pi -m openai-codex/no-such-model -p 'say hi'
```

Alternatively, temporarily remove auth for a real model. Expect terminal `failed` with
a readable provider/auth/model error in `state.json` and `report.md`, not a report made
only of lifecycle JSON, and no indefinite wait for `agent_end` after a failed response.

### S15: Runtime diagnostics identify the auth source

With an authenticated real `pi` on `PATH` and no override, expect launch diagnostics to
include runtime kind `path`, the binary path and version, session directory, and
`pi_runtime_auth_policy: shared-pi-agent-dir`. `PI_CODING_AGENT_DIR` points to the
Pi-owned agent tree.

### S16: Missing or incompatible runtime fails before launch

```bash
PATH=/empty meridian --harness pi -m <pi-model>
MERIDIAN_PI_BINARY=/bad/pi meridian spawn --harness pi -m <pi-model> -p "hi"
```

Expect explicit not-installed/PATH guidance for the first case and incompatible-binary
guidance for the second. Neither launches a bundled or fake fallback.

### S17: Spawn show exposes the latest Pi phase

```bash
uv run meridian spawn --harness pi -m <pi-model> --bg -p "Reply OK and run no commands."
uv run meridian spawn show <spawn-id> --verbose
```

Expect `Pi phase:` while running or after the bounded post-terminal history wait. Useful
current phases include `waiting_for_first_pi_event_after_prompt`,
`waiting_for_tracked_children`, `waiting_for_notification_completion`,
`quiescence_micro_drain_started`, `pi_child_wave_timeout`,
`pi_notification_timeout`, `finalized`, and the asynchronous `cleanup_*` diagnostics.

### S18: PATH and override select the same real runtime

Run one primary and one spawned launch using default `PATH` resolution, then repeat with
`MERIDIAN_PI_BINARY` set to that same real binary. Diagnostics distinguish runtime kind
`path` from `override`; auth remains in the same Pi-owned agent tree.

### S19: Phase diagnostics distinguish completion from teardown

For a successful spawned RPC launch, inspect verbose status and `history.jsonl` during
startup, waiting, terminal publication, and teardown. Expect completion to publish and
the completion state to reach `finalized`, with no `cleaning` phase. Any
`cleanup_running`/`cleanup_completed`/`cleanup_escalated`/`cleanup_failed` events belong
to asynchronous `PiDrainSessionTeardown`, not terminal-state gating.

## Disk and internal lifecycle mechanics (S20-S28)

Use S6-style real tracked work and inspect:

- `<runtime-root>/spawns/<spawn-id>/state.json`
- `<runtime-root>/pi-bash/<spawn-id>/bash-records.json`
- `<runtime-root>/pi-bash/<spawn-id>/last-notification.json`
- `<runtime-root>/spawns/<spawn-id>/history.jsonl`

### S20: Private disk state is ingested

Managed-bash and notification file changes wake the drain and are reflected in
quiescence decisions. Persisted descendants are read separately from valid,
parent-linked spawn rows; the private disk watcher is not their authority.

### S21: Invalid or incomplete state is not treated as quiescent

Unreadable private files produce typed unknown evidence. Incomplete or wrong-parent
spawn directories do not become descendants, while a valid live grandchild beneath a
terminal direct child still blocks completion. Use the integration coverage under
`tests/integration/streaming/` for destructive race and parse-error injection.

### S22: Stderr and duplicate lifecycle input are non-authoritative

Incidental Pi stderr remains in stderr logs and cannot drive quiescence. Lifecycle
messages can supply rowless child and process-handle evidence, but cannot override the
reconciled persisted descendant tree; duplicate transitions are deduplicated.

### S23: A completed child wave produces an aggregate continuation

Start multiple tracked jobs that finish close together. Expect one aggregate
notification/continuation for the wave rather than one competing parent turn per child.

### S24: Successive legitimate child waves may extend total duration

Have a continuation start a second tracked wave. Each wave gets its own anchored
child-wave window. Ordinary activity does not slide a current wave's deadline, but a
new legitimate wave can establish a new one; therefore total Pi completion duration is
intentionally unbounded unless the outer attempt timeout is opted in.

### S25: Detached work stays outside the blocker set

Mix tracked and detached jobs. Completion waits for tracked descendants/private work
and their notification, never for the detached jobs.

### S26: Notification deadline is anchored

If a notification remains queued or delivered without completing, its own anchored
deadline yields terminal `failed` with `pi_notification_timeout`. Ordinary activity
does not slide that deadline. Exercise fault timing with
`tests/integration/streaming/test_pi_characterization.py` rather than a shimmed manual
runtime.

### S27: Child-wave deadline is not the absolute attempt timeout

A parent that is idle with tracked work past the configured child-wave deadline fails
with `pi_child_wave_timeout`; cleanup is latched once and runs after publication. This
per-wave safety bound is distinct from `--timeout`/`MERIDIAN_TIMEOUT`, which caps the
entire attempt and produces `timed_out`.

### S28: Mixed-wave cleanup preserves the terminal reason

Combine successful, failing, and detached children. A child-wave or notification
timeout remains the terminal reason even if asynchronous cleanup later fails. Cleanup
failure is diagnostic and does not restart waiting phases or replace the published
outcome.

## Primary TUI and session identity (S29-S36)

### S29: Primary loads the enabled interactive integrations

```bash
uv run meridian --harness pi -m <pi-model>
```

Expect primary argv to load `meridian-spawn-watch` and, by default,
`managed-bash` (controlled by `harness.pi.background_tasks.enabled`). It does not use
spawned RPC's `--mode rpc` or blanket `--no-extensions` projection.

### S30: Primary child completion can notify without auto-finalizing

From the native TUI, start `meridian spawn` child work and wait for it. Child completion
can trigger a native Pi follow-up, but primary remains in the TUI until the user exits;
spawned RPC quiescence does not auto-stop it.

### S31: Coordination stays out of the visible TUI

Disk-backed Pi files carry coordination. Raw fields and events such as `parent_id`,
`originating_bash_id`, notification markers, and `meridian.notification.*` must not
appear as user-visible TUI output.

### S32: Primary diagnostics do not drive spawned quiescence

Primary wrapper/session diagnostics are best-effort metadata only. They cannot act as
spawned RPC lifecycle evidence or terminal authority.

### S33: Primary discovery records the real Pi session UUID

Send at least one primary prompt, exit, and inspect
`meridian spawn show <primary-spawn-id>`. Meridian discovers flat
`PI_CODING_AGENT_SESSION_DIR/*.jsonl` files by cwd and launch time; it does not assume a
cwd-bucketed layout.

### S34: Continue projects to native Pi session selection

```bash
uv run meridian --continue <chat-id-from-primary>
```

Expect projection to native `pi --session <uuid>` for the discovered session.

### S35: Fork projects to native Pi fork selection

```bash
uv run meridian --fork <chat-id-from-primary>
```

Expect projection to native `pi --fork <uuid>`.

### S36: Session-discovery failures remain distinct

If Pi created no session, continue/fork fails with a "no Pi session was created"
diagnostic. If candidate files exist but cannot be matched or parsed, expect a distinct
session-discovery diagnostic rather than silently choosing an unrelated session.

## Retired scenarios

None. All S1-S36 coverage remains represented; obsolete expectations were rewritten to
the current completion, timeout, and teardown model.
