# qa-validated: pi-rpc-quiescence

# Pi RPC quiescence smoke and diagnostics (S1-S40)

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
absolute ceiling for the whole attempt. The child-wave deadline is a separate,
wave-local safety bound.

The completion state machine reaches `finalized`; there is no `cleaning` phase.
Cleanup lifecycle rows (`cleanup_running`, `cleanup_completed`, `cleanup_escalated`,
or `cleanup_failed`) are best-effort diagnostics. They come from Pi's
`pi_drain_teardown.py`; `drain_teardown.py` is the harness-neutral teardown contract.
History orders `finalized` before cleanup, although store-level `published_at` can
currently trail cleanup rows (#431).

**Phase inspection rule:** after any surface first shows a terminal outcome, poll
`history.jsonl` for a bounded interval before asserting on `finalized` or cleanup rows.

**Prompt-steering validity rule:** if the transcript shows that the model bypassed the
seam under test—for example, by waiting or polling in the same turn, or by using the OS
`timeout` command instead of the bash tool's timeout parameter—the scenario was not
exercised. Rerun it; do not record a verdict from that attempt.

---

# Tier 1 — must-run smoke

Run S1-S12 before merging a Pi-related change. S9 and S11 are fault contracts with
automated coverage; inspect them rather than corrupting a real Pi installation.

## S1: Basic Pi RPC spawn

```bash
uv run meridian spawn --harness pi -m <pi-model> -p "Reply OK and run no commands"
```

Expect a normal reply, terminal status `succeeded`, no wait on nonexistent child work,
and a `finalized` history row.

## S2: Managed bash blocking command

Prompt:

```text
Run `printf hello` with bash and report the output.
```

Expect the `tool_execution_end` result to contain `result.details.exit_code: 0`,
`result.details.stdout` containing `hello`, and an empty
`result.details.stderr`. Blocking-command results do not include a `state` field.
A short blocking command creates no tracked descendant or follow-up work.

## S3: Bash-tool timeout becomes tracked work and drains

Prompt:

```text
Run `sleep 5 && echo done` with the bash tool, setting the bash tool's timeout parameter to 1000ms. Do not wrap the command in the `timeout` program. Report the job id and end your turn immediately — do not wait for or poll the job.
```

Expect the tool call to return `state: "running"` and a `job_id`. The disk-backed bash
record moves from running to exited with stdout `done`. This bash-tool timeout does
**not** set Meridian's absolute attempt timeout: the parent remains active through the
follow-up turn, then succeeds after that turn quiesces. Canonical
`meridian.subspawn.*` telemetry is not part of the runtime contract.

## S4: Detached job does not block quiescence

Prompt:

```text
Start `sleep 30` in the background with wait_policy detached, then say DETACHED and end your turn immediately. Do not wait for the job.
```

Expect the bash record to use `wait_policy: "detached"` and the session to quiesce
without waiting 30 seconds. Detached work is user-owned and is not killed by Pi
quiescence cleanup.

## S5: RPC multi-turn manual injection

```bash
uv run meridian spawn --harness pi -m <pi-model> --bg -p "Reply FIRST and wait."
uv run meridian spawn inject <spawn-id> "Reply SECOND."
uv run meridian spawn wait <spawn-id>
```

Issue the inject immediately, while the first turn is busy. Expect the CLI to wait for
the live spawn's control endpoint, Pi to queue the message as a follow-up turn, and one
Pi process to complete after the second turn quiesces. Injection into a terminal spawn
must instead fail with an honest not-running/connection-refused error.

## S6: Tracked child completion wakes the parent

Append this instruction to both S6 prompts: `End your first turn immediately without
waiting for or polling the job. When completion wakes you, summarize the result.` The
transcript must show a later follow-up turn that handles the result.

Prompt:

```text
Start `sleep 3 && echo child-done` as tracked background work.
```

Expect the disk-backed record at
`<runtime-root>/pi-bash/<spawn-id>/bash-records.json` to move from running to exited
with stdout `child-done`; expect `last-notification.json` to name the completed work,
a `message_start`/`message_end` pair with `customType: meridian-spawn-watch`, and a
follow-up turn that reports the result. Completion waits for that turn to quiesce.
Do not expect canonical `meridian.notification.*` or `meridian.subspawn.*` rows.

### S6b: Fast tracked completion still triggers a follow-up

Prompt:

```text
Start `sleep 0.1 && echo fast-done` as tracked background work.
```

The record may reach exited before the first `agent_end`. Even then, expect stdout
`fast-done`, a follow-up that reports it, and completion only after that follow-up.

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
Start `sh -c 'sleep 1; exit 7'` as tracked background work, then end your turn immediately. Do not wait for or poll it in this turn. Handle the result when its notification wakes you.
```

Expect the bash record to reach exited with exit code 7, then a follow-up turn to handle
the failure. `last-notification.json` names the completed work and the direct
`meridian-spawn-watch` custom message starts the follow-up. Completion waits for that
turn to quiesce; handling the failure can still produce a successful parent outcome.
Canonical notification/subspawn lifecycle rows remain absent.

## S9: Follow-up completion has no canonical-event timeout dependency

Do not inject shims into this real-runtime guide. The contract is covered by
the SpawnManager/drain-loop regression in
`tests/integration/streaming/test_pi_manager_retained_paths.py`; the lower-level
row/marker/idle-epoch assertions remain in
`tests/integration/streaming/test_pi_characterization.py`.

The covered contract is the real runtime shape: a persisted child becomes terminal,
`last-notification.json` advances, a direct `meridian-spawn-watch` custom message starts
the follow-up turn, and the parent completes after that turn without canonical
notification/subspawn lifecycle rows. There is no `pi_notification_timeout` branch.
If the direct follow-up never arrives, use the outer attempt timeout to bound the run;
the outcome is `timed_out`, not a synthetic canonical-notification failure.

## S10: Pi exits with tracked work pending

```bash
uv run meridian spawn --harness pi -m <pi-model> --bg -p "Start tracked background work: sleep 30."
# After the managed-bash disk record shows the tracked job running:
kill <pi-process-pid>
uv run meridian spawn wait <spawn-id>
```

Expect terminal `failed` with `pi_process_exited_with_tracked_children`, rather than
success from an earlier `agent_end`. Best-effort cleanup cancels persisted descendants;
it does not depend on lifecycle PID/PGID telemetry. Detached work is not killed. The
dead spawn's private bash record may remain `running` post-mortem and is not authoritative.

## S11: Malformed coordination state fails closed

Do not corrupt a normal smoke environment. Malformed lifecycle parsing is covered by
`tests/unit/harness/test_pi_integration.py`; unreadable/private-disk evidence and
recovery behavior are covered under `tests/integration/streaming/`.

The contract is typed unknown evidence—not an empty/no-work snapshot. A completion
candidate waits for the single relevant completion deadline and fails explicitly if
the evidence remains unreadable.

## S12: Opt-in absolute attempt timeout — both carriers

Use a deliberately long tracked job and a short **minutes-valued** attempt ceiling.
Run the scenario **twice**, once per carrier:

```bash
uv run meridian spawn --harness pi -m <pi-model> --timeout 0.05 \
  -p "Start tracked background work: sleep 9999."
MERIDIAN_TIMEOUT=0.05 uv run meridian spawn --harness pi -m <pi-model> \
  -p "Start tracked background work: sleep 9999."
```

For **each** carrier expect the resolved policy snapshot, `state.json`, CLI report, and
history `finalized` row to agree on `timed_out`, exit code 3, and error `timeout`, never
Pi's induced `cancelled`/130. Cleanup rows follow `finalized`. Visible terminal
publication lagged 19-26 seconds in the verification pass; shortening it remains #431.
With a three-second ceiling the model may not start `sleep 9999`, so check for an
orphan only when the bash record says it started. Detached jobs remain user-owned.
Without `--timeout` or `MERIDIAN_TIMEOUT`, Pi has no equivalent total wall-clock ceiling
while legitimate descendant waves continue.

---

# Tier 2 — deep diagnostics appendix

Run the cluster that matches the failure or code being changed.

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
`waiting_for_tracked_children`, `quiescence_micro_drain_started`,
`pi_child_wave_timeout`, `finalized`, and the asynchronous `cleanup_*` diagnostics.

### S18: PATH and override select the same real runtime

Run one primary and one spawned launch using default `PATH` resolution, then repeat with
`MERIDIAN_PI_BINARY` set to that same real binary. Diagnostics distinguish runtime kind
`path` from `override`; auth remains in the same Pi-owned agent tree.

### S19: Phase diagnostics distinguish completion from teardown

For a successful spawned RPC launch, inspect verbose status and `history.jsonl` through
startup, waiting, and terminal/teardown convergence. Expect `finalized`, no `cleaning`
phase, and then any `cleanup_running`/`cleanup_completed`/
`cleanup_escalated`/`cleanup_failed` rows from `PiDrainSessionTeardown`; those rows do
not gate the terminal outcome.

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

### S22: Stderr and retired lifecycle input are non-authoritative

Incidental Pi stderr remains in stderr logs and cannot drive quiescence. Retired
notification/subspawn lifecycle-shaped messages cannot supply child or process-handle
evidence. The reconciled persisted descendant tree remains the sole child authority.

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
and the direct follow-up turn, never for the detached jobs.

### S26: Notification marker is an epoch gate, not a deadline

After the parent goes idle, a newer `last-notification.json` marker keeps the current
completion candidate from finalizing. The direct `meridian-spawn-watch` custom message
starts a follow-up turn; its later idle epoch is newer than the marker, so completion may
proceed. There is no canonical-event deadline and no `pi_notification_timeout` outcome.
Exercise the persisted child → terminal row → marker → custom follow-up sequence with
`test_spawn_manager_derives_direct_followup_transitions_from_pi_events`; use
`test_real_pi_tracked_child_followup_has_no_canonical_lifecycle_dependency` for the
lower-level row/marker/idle-epoch contract rather than a shimmed manual runtime.

### S27: Child-wave deadline is not the absolute attempt timeout

A parent that is idle with tracked work past the configured child-wave deadline fails
with `pi_child_wave_timeout`; cleanup is latched once and follows `finalized`. This
per-wave safety bound is distinct from `--timeout`/`MERIDIAN_TIMEOUT`, which caps the
entire attempt and produces `timed_out`.

### S28: Mixed-wave cleanup preserves the terminal reason

Combine successful, failing, and detached children. A child-wave timeout remains the
terminal reason even if asynchronous cleanup later fails. Cleanup
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

Disk-backed Pi files carry coordination. Raw fields such as `parent_id`,
`originating_bash_id`, and notification markers must not
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

## Timeout ceiling, teardown ordering, and store composition (S37-S40)

Run this cluster when changing attempt-timeout plumbing (`streaming_runner.py`,
execution policy resolution), teardown (`drain_teardown.py`/`pi_drain_teardown.py`),
or the state store/locking layer under the drain.

### S37: Ceiling does not misfire on fast completions

```bash
uv run meridian spawn --harness pi -m <pi-model> --timeout 2 \
  -p "Reply OK and run no commands."
```

A run finishing well under its armed ceiling must terminate `succeeded`, never
`timed_out`; two minutes leaves headroom above the observed 20-30-second trivial-run
baseline.

### S38: Detached work survives the attempt timeout

```bash
uv run meridian spawn --harness pi -m <pi-model> --timeout 1 \
  -p "Immediately start 'sleep 300' in the background with wait_policy detached, then immediately start tracked background work: sleep 9999. End your turn as soon as both are started; do not wait for either."
```

Use one minute so the model can start both jobs before the ceiling. Verify from
`pi-bash/<spawn-id>/bash-records.json` that both jobs started, then expect the
parent to reach `timed_out`, the **tracked** `sleep 9999` to
be cleaned up, and the **detached** `sleep 300` to still be alive in the
process table after completion. Kill the detached sleep afterwards.

### S39: Finalization and cleanup ordering, on disk

For one successful S1-style run and one S12 timed-out run, read
`spawns/<spawn-id>/history.jsonl` and `state.json`. Assert that every row has a
sub-second `timestamp`, terminal state has sub-second `published_at`, and each cleanup
row has both a higher `seq` and later `timestamp` than `finalized`. Both runs must have
`finalized` and cleanup rows, and neither may contain a `cleaning` completion phase.
Store-level `published_at` can trail drain cleanup rows; fixing that ordering remains
#431.

### S40: Drain under concurrent read-path load

```bash
uv run meridian spawn --harness pi -m <pi-model> --bg -p "Start tracked background work: sleep 5. Summarize when done."
uv run meridian spawn --harness pi -m <pi-model> --bg -p "Start tracked background work: sleep 5. Summarize when done."
```

While both drain, poll `uv run meridian spawn show <spawn-id>` and
`uv run meridian spawn list` against both IDs roughly every second until terminal.
Expect no lock errors, no hangs, no torn or unparseable `state.json` reads, both
spawns reaching clean terminal states, and `spawn wait` returning for each. The
drain's write path and the CLI read path cross the store's locking seams — this
scenario exercises that composition, which neither layer's own tests see.

## Retired scenarios

None. All S1-S40 coverage remains represented; obsolete expectations were rewritten to
the current completion, timeout, and teardown model.
