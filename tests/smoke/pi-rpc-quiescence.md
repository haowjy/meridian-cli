# Pi RPC quiescence smoke and diagnostics

**Opt-in live tier:** these scenarios run a real Pi process and may invoke a
paid provider. They are not part of automatic smoke. Run the short manual gate
first and use only disposable state.

Use a **real** installed Pi and a cheap model. Run the
[manual gate](pi-manual.md) first. These scenarios verify current CLI workflows;
automated fault coverage is listed separately below.

## Setup and evidence

From the repository root in a fresh shell, prepare dependencies (network may
be needed), then create one fixture:

```bash
(
  cd src/meridian/pi_runtime
  pnpm install --frozen-lockfile
)
. tests/smoke/scripts/pi-setup.sh --build-extensions
pi --version
pi --help
```

Use Node 24+ (matching CI), a compatible `pi` on `PATH`, and deliberately supplied
provider auth as described in the [manual gate](pi-manual.md). Copy only
`auth.json`, not the native agent tree, or supply the provider's API-key env var.
`pi-setup.sh` isolates stores and task/control dirs below `SMOKE_ROOT`; extension
bundles are read from the package. Managed prelaunch pins task state to the
isolated project runtime. Finish/cancel all work before `smoke_cleanup`.

Inspect a run with:

```bash
uv run meridian spawn show <p-id> --verbose
uv run meridian --json spawn show <p-id>
uv run meridian session log <p-id>
```

Authoritative/private files under the project runtime:

| Path | Evidence |
|---|---|
| `spawns/<p-id>/state.json` | Canonical spawn status, parent/origin, metrics, run boundary |
| `spawns/<p-id>/pi-lifecycle.json` | Latest bounded drain/cleanup phase diagnostics |
| `spawns/<p-id>/pi-session-boundary.json` | Nonce/PID-correlated native lifecycle observations |
| `pi-bash/<p-id>/bash-records.json` | Shell ownership/tracking, terminal and consumption state |
| `pi-bash/<p-id>/delivery-receipts.json` | Exact native custom-message admission membership |
| `pi-bash/<p-id>/delivery-observations.json` | Python's observation of the matching public event |
| `pi-bash/<p-id>/observed-spawns.json` | Explicit child consumption and live wait leases |
| `pi-bash/<p-id>/delivery-fault.json` | Supervised delivery errors |

Native `session log` supplies conversation evidence. Meridian does not write a
runner `history.jsonl`; do not poll one for phases. A queued follow-up is not
admission. `last-notification.json` is ignored and cannot prove delivery. See the
[delivery contract](../../src/meridian/pi_runtime/.context/delivery-contract.md).

A model that waits/polls in the same turn instead of exercising an unattended
notification has not tested the notification path. Check the transcript and rerun
with clearer steering rather than marking that scenario passed.

## Core spawned workflows

### Basic turn and blocking Bash

```bash
uv run meridian spawn --harness pi -m <pi-model> \
  -p 'Reply OK and run no commands.'
```

Expect `succeeded` and a normal report, with no nonexistent-child wait. Repeat with
`Run printf hello using bash and report the output.` A foreground result exposes
`exit_code`, `stdout`, and `stderr`; it creates no unattended completion obligation.

### Unattended background result

Prompt:

```text
Use bash(command="sleep 3 && echo child-done", background=true).
Report its b-* ID and end this turn without waiting or polling it.
When completion wakes you in a later turn, summarize the result.
```

Expect a `bash_id`, initially `status: started`, then a terminal tracked background
row. The native `meridian-spawn-watch` custom message carries a delivery ID and work
membership, matched by its receipt and public observation. The parent handles that
follow-up and becomes idle before success. Repeat with `sleep 0.1` to cover a task
finishing before the initial turn ends, and with `sh -c 'sleep 1; exit 7'` for a
failure result. Handling a child/task failure can still produce a successful parent.

### Explicit wait consumes once

Prompt:

```text
Start sleep 2 && echo waited using bash with background=true. Use bash_manage
with action=wait and that b-* ID. Report waited and run no further work.
```

Expect `notification_consumed_at_ms` persisted before the terminal wait returns,
with no second completion notice for the waited result. An active wait defers
notification publication. In `/ps`, clearing terminal history preserves unread
background results but can remove returned foreground or consumed results.

### Foreground timeout backgrounds the command

Prompt:

```text
Run sleep 65 && echo timeout-done using bash with timeout_min=1.
If it returns a backgrounded b-* ID, end the turn without waiting or polling.
Handle its later completion notice.
```

`timeout_min` is minutes (schema range 1–59), not milliseconds. Expect
`status: backgrounded`, continued process ownership, and a later completion notice.
This tool timeout does not set Meridian's absolute attempt timeout.

### Detach releases tracking, not ownership

Prompt:

```text
Use bash to start sleep 30 with background=true. Use bash_manage(action=detach)
for the returned b-* ID, then reply DETACHED and end the turn.
```

Expect `is_tracked=false`, so the task no longer blocks quiescence. A live owner
still terminates its process group during normal Pi shutdown; detach does not make
it user-owned or promise survival after timeout/shutdown.

### Nested local-source child

Run from the checkout root:

```bash
PROJECT=$PWD
MODEL='<eligible-pi-model>'  # choose a cheap Pi-native ID from the live listing
timeout 240s uv run meridian -C "$PROJECT" --harness pi spawn \
  -m "$MODEL" \
  -p "Run exactly this command as a child spawn and wait for it: uv run meridian -C '$PROJECT' --harness pi spawn -m '$MODEL' -p 'Reply exactly NESTED_CHILD_OK and run no commands.' --timeout 1 --format json. After it completes, reply exactly PARENT_AFTER_CHILD_OK and include the child spawn id. Run no other commands." \
  --timeout 4 --format json
```

Expect both markers and successful canonical child/parent rows. The child has the
parent's `parent_id` and, when launched by managed Bash, a `b-*`
`originating_bash_id`. Result consumption is explicit here; use an unattended
background launch to test child completion notifications. Parent completion follows
the reconciled **transitive** tree, including live grandchildren beneath terminal
direct children, not shell-command parsing.

### Control injection

```bash
uv run meridian spawn --harness pi -m <pi-model> --bg \
  -p 'Run sleep 10 with bash, then reply FIRST.'
uv run meridian spawn inject <p-id> 'Reply SECOND.'
uv run meridian spawn wait <p-id>
```

Inject while the initial turn is active. Expect a queued follow-up in the same Pi
process and success after it settles. Injection into a terminal spawn must fail
honestly, not launch another session.

## Primary TUI and native identity

```bash
uv run meridian --harness pi -m <pi-model>
```

Expect the real native TUI, no `--mode rpc`, and all three default Meridian bundles.
Primary retains ambient discovery. Child notifications do not apply spawned
quiescence auto-stop; the user controls TUI exit.

Start background Bash, run `/reload`, and inspect `/ps` plus task output. Expect
live ownership and queued delivery claims to survive reload. Kill/abort waits for
owned process groups and queued output; detached groups are still owned.

Send a prompt, exit, and inspect `chat_id`, `continue_chat_id`, and `run_boundary`.
The entry is assigned before exec. Only the boundary record's matching nonce/PID
and final readable quit verify exit; no cwd/time/newest-file discovery is allowed.
Exercise a native session switch and quit when that Pi version exposes it. Confirm
that a verified exit change does not mutate the entry chat's native binding.

```bash
uv run meridian --continue <chat-id> --dry-run --json
uv run meridian --fork <chat-id> --dry-run --json
```

Resume projects the exact recorded `--session <file>` and `--session-dir`.
Fork projects `--fork <source-file>`, a newly assigned `--session-id`, and its target
store. Missing/ambiguous sources refuse; an unresolved exit cannot be repaired by
selecting a recent transcript.

## Deadlines, failures, and cleanup

Pi has **no default total wall-clock ceiling**. Each legitimate child wave can
establish its own anchored deadline. Tracked Bash execution alone does not start
the persisted-child wave timer. Owed delivery/publication and unreadable evidence
have separate bounded recovery windows. `done` cannot skip an active native turn,
an owed result, or unknown evidence.

### Absolute timeout, both carriers

Run separately:

```bash
uv run meridian spawn --harness pi -m <pi-model> --timeout 0.05 \
  -p 'Use bash to start sleep 9999 with background=true, then end the turn.'
MERIDIAN_TIMEOUT=0.05 uv run meridian spawn --harness pi -m <pi-model> \
  -p 'Use bash to start sleep 9999 with background=true, then end the turn.'
```

Both carriers are minutes-valued, non-renewing attempt ceilings. Expect `timed_out`,
exit code 3, error `timeout`, not induced cancellation/130. The model may not start
the task before three seconds; check records before claiming process-cleanup
coverage. Repeat with enough time for the task to start when testing cleanup, and
confirm both tracked and detached owned groups are gone after normal shutdown.
A fast run under a generous ceiling must still succeed.

### Child waves and early Pi exit

For a parent that goes idle with a real child spawn still active, use a deliberately
short configured `timeouts.pi_child_wave_timeout_seconds`. Expect
`pi_child_wave_timeout`, a latched failed outcome, and one best-effort descendant
cleanup. Successive legitimate waves can extend total duration; ordinary disk
activity cannot slide a wave's deadline. Delivery failures preserve their own
`pi_delivery_unresolved` / `pi_evidence_unreadable` reasons.

In an isolated probe, terminate Pi after a tracked task/descendant is demonstrably
running. Expect an explicit process/unfinished-work failure, not success from an
earlier idle event. Inspect descendants and process groups before cleanup; SIGKILL
cannot run extension shutdown handlers. Cold recovery never signals recorded PIDs.

### Provider failure and read-path load

An invalid Pi-native model or missing auth must produce a readable failure report,
not only lifecycle JSON or an indefinite prompt-ACK wait. Missing/incompatible
installed Pi must fail before launch with installation/update guidance.

While two real parent runs drain tracked work, poll `spawn show` and `spawn list`
about once per second. Expect parseable rows, no lock errors or hangs, and terminal
wait results. Inspect latest `pi-lifecycle.json`/verbose status for cleanup; it is
bounded latest-state evidence, not an append-only phase sequence. Cleanup diagnostics
must not replace the causal terminal outcome.

## Automated fault coverage

Use existing fixture coverage rather than corrupting a live parent or installation:

- `tests/integration/launch/test_pi_run_boundary.py`: actual built boundary records,
  stale-context/switch/quit behavior, entry mismatch, and exit attribution.
- `tests/integration/harness/`: RPC receive/write/ACK/EOF, metadata publication,
  native identity, and transport failure shapes.
- `tests/integration/streaming/`: malformed/missing private evidence, causal result
  fencing, transitive descendants, refresh races, done, and anchored deadlines.
- `src/meridian/pi_runtime/extensions/managed-bash/`: reload/recovery, process groups,
  supervised storage failures, and explicit-wait notification deduplication.
- `src/meridian/pi_runtime/extensions/meridian-spawn-watch/`: native idle deferral,
  partial consumption, admission receipts, fair scans, and reload ownership.

Cold recovery retains terminal history and consumption; prior running tasks report
`ownership_lost` until explicit detach/manual recovery. An admission receipt without
its matching public observation fails closed after bounded recovery. Do not delete
receipts or fabricate observations to force completion. No atomic exactly-once
promise spans a process crash. See [0.9 upgrade notes](../../docs/upgrading.md).
