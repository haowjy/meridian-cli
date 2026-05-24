# qa-validated: pi-rpc-quiescence

# Pi RPC quiescence smoke (S1-S36)

Manual smoke scenarios for Pi RPC quiescence behavior. All scenarios assume a **real**
installed `pi` binary — no stub scripts, shims, or fake runtimes.

Run the short gate in `tests/smoke/pi-manual.md` first.

## Prerequisites

```bash
. tests/smoke/scripts/pi-setup.sh --build-extensions
```

- **Node 24+** on `PATH` (extension build; matches CI)
- **Real `pi` on `PATH`** — `pi --version` and `pi --help` succeed; spawned runs need
  `--mode rpc` in help output
- **Provider auth** under `~/.pi/agent`. Meridian sets `PI_CODING_AGENT_DIR` to that
  agent tree (default `~/.pi/agent`) for every Pi subprocess via `pi_paths`
- **Spawn sessions:** `~/.meridian/meridian-pi/sessions/<spawn-id>/` (honors
  `MERIDIAN_HOME` when set)
- **Extensions:** `~/.pi/agent/extensions/meridian/<launch-id>/` materialized per launch
- **Optional:** `MERIDIAN_PI_BINARY=/path/to/real/pi` only when your install is not on
  `PATH` (must be the same compatible `pi` binary, not a test harness script)
- Branch includes Pi RPC wiring + built extensions; run spawned checks from this repo
  root when exercising background tasks

---

## S1: Basic Pi RPC spawn

```bash
uv run meridian spawn -m <pi-model> -p "Reply OK and run no commands"
```

Expect:
- Agent responds normally
- Spawn reaches `succeeded`
- Logs show clean quiescent stop path (no hang waiting on child jobs)

## S2: Managed bash blocking command

Prompt:

```text
Run `printf hello` with bash and report the output.
```

Expect:
- `bash` returns `state: "exited"`, `exit_code: 0`, output includes `hello`
- No `meridian.subspawn.start` event for this short command

## S3: Timeout detaches and drains

Prompt:

```text
Run `sleep 5 && echo done` with bash using timeout 1000ms, then report the job id.
```

Expect:
- `bash` returns `state: "running"` with `job_id`
- Raw output includes `meridian.subspawn.start` (`wait_policy:"tracked"`)
- Later includes `meridian.subspawn.end`
- Spawn remains alive until tracked job drains, then finalizes

## S4: Detached job does not block quiescence

Prompt:

```text
Start `sleep 30` in the background with wait_policy detached, then say DETACHED.
```

Expect:
- Start event uses `wait_policy:"detached"`
- Session can quiesce without waiting the full 30s
- Detached job remains user-owned responsibility

## S5: RPC multi-turn manual injection

```bash
uv run meridian spawn -m <pi-model> --bg -p "Reply FIRST and wait."
meridian spawn inject <spawn-id> "Reply SECOND."
meridian spawn wait <spawn-id>
```

Expect:
- One spawned Pi process handles both turns
- Spawn completes after second turn quiesces

## S6: Tracked child completion wakes parent

Prompt:

```text
Start `sleep 3 && echo child-done` as tracked background work. When it completes, summarize the result.
```

Expect:
- Parent turn idles while child runs
- Lifecycle emits `meridian.notification.queued` + `delivered`
- Internal extension bus receives `meridian:task:start` / `meridian:task:end` from background-tasks (and `meridian:subspawn:*` for spawn wrappers)
- Follow-up turn starts automatically
- After follow-up `agent_end`, quiescence is reached and spawn completes

## S6b: Fast tracked child completion still triggers follow-up

Prompt:

```text
Start `sleep 0.1 && echo fast-done` as tracked background work. Continue immediately, then summarize when background work completes.
```

Expect:
- Child start and end can both happen before the first `agent_end`
- Lifecycle still emits `meridian.notification.queued` + `delivered` (not only `meridian.quiescence.ready`)
- Follow-up turn runs and reports `fast-done`
- Spawn reaches quiescence only after the follow-up turn completes

## S6c: Nested auto-wait stale detection is bounded

Setup:

1. Start a spawned Pi RPC run that launches tracked work, then force-kill the
   runner process after the spawn row is created (fault-injection path).
2. From a nested Meridian shell (`MERIDIAN_DEPTH=1`), run:

```bash
meridian spawn wait <spawn-id>
```

Expect:
- Stale shaping applies only after grace windows: startup grace (~15s) and
  recent-activity grace (~120s heartbeat/history/lifecycle mtime). Before those
  windows expire, nested wait/show can still report `running`.
- Wait does not hang forever in nested mode
- `spawn show`/`spawn wait` resolves using read-only stale detection
- Status surfaces as failed with `stale_nested_read` (or
  `stale_nested_read_no_pid` when runner pid metadata is absent)
- On-disk spawn row is not reconciler-finalized by the nested read path; verify
  `<runtime-root>/spawns/<spawn-id>/state.json` still has active status
  (`"status":"running"`) after nested `spawn wait` returns synthetic failed

## S7: Primary Pi native TUI wrapper

```bash
uv run meridian --harness pi -m <pi-model>
```

Expect:
- Meridian launches Pi's native TUI command without `--mode rpc`
- Meridian does not instantiate `PiRpcConnection` for primary and does not run a fake `input("> ")` loop
- Human input/rendering happens inside Pi's native TUI, not through Meridian RPC prompt translation
- Meridian records best-effort native wrapper metadata: primary spawn id, pid, cwd, session dir, exit status, and discovered Pi session id when available
- No spawned quiescence or RPC `stop(reason="quiescent")` is applied to the primary session

## S8: Child failure notifies

Prompt:

```text
Start `sh -c 'sleep 1; exit 7'` as tracked background work. Handle the result.
```

Expect:
- End event shows non-zero failure (`success:false`, `exit_code:7`)
- Parent receives failure follow-up turn
- Spawn outcome depends on parent handling path (successful handled flow vs terminal error)

## S9: Notification delivery failure does not hang

**Automated coverage:** `tests/unit/streaming/test_pi_quiescence.py` (notification
delivery faults). Manual smoke does not use extension shims or env monkey patches.

If you observe a real spawn where a follow-up notification cannot be delivered, expect
spawn to finalize `failed` once tracked children drain — not hang waiting on a pending
notification. History may include `meridian.notification.queued` and
`meridian.notification.failed`.

## S10: Pi dies with tracked child pending

Setup:

```bash
uv run meridian spawn -m <pi-model> --bg -p "Start tracked background work: sleep 30."
# after meridian.subspawn.start appears:
kill <pi-process-pid>
meridian spawn wait <spawn-id>
```

Expect:
- Spawn finalizes failed, not succeeded from the earlier `agent_end`
- Failure reason mentions tracked children / Pi process exit
- POSIX cleanup attempts tracked process-group termination when pid/pgid metadata exists
- Detached jobs are not killed by the cleanup path

## S11: Malformed lifecycle event fails closed

**Automated coverage:** `tests/unit/harness/test_pi_integration.py` and
`tests/unit/streaming/test_pi_quiescence.py` (malformed sidecar lifecycle lines).

Manual smoke does not inject malformed JSON into the lifecycle sidecar.

## S12: Hard-kill deadline

Prompt with a deliberately long tracked job and low spawn timeout:

```bash
uv run meridian spawn -m <pi-model> --timeout 3 -p "Start tracked background work: sleep 9999."
```

Expect:
- Session stays active until the timeout/deadline
- Meridian cancels/stops Pi and attempts tracked job cleanup
- Spawn finalizes timeout/failed, not succeeded
- Detached jobs remain user-owned and are not killed by quiescence cleanup

## S13: No pre-prompt session event probe

Probe raw Pi RPC without sending a prompt:

```bash
(sleep 5) | timeout 3s pi --mode rpc --model <pi-model> --no-extensions --no-skills --no-context-files --no-prompt-templates
```

Expect:
- Probe may produce no stdout before timeout
- This confirms Meridian must not wait for a `session` event before writing the first prompt

## S14: Prompt response failure does not hang

Use **real** `pi` with missing or invalid provider auth (or an invalid model id):

```bash
meridian spawn --harness pi -m openai-codex/no-such-model -p 'say hi'
# or: break/remove auth under ~/.pi/agent, then retry a model that requires it
```

Expect:
- Spawn status `failed` with a readable provider/auth/model error in `state.json` /
  `report.md`
- Report is not only `cleanup_completed` lifecycle JSON (see `tests/smoke/pi-manual.md`
  #262 check)
- Spawn does not hang waiting for `agent_end` after a failed `response`

## S15: Runtime selection explains auth source

Setup:
- Real `pi` on `PATH` is authenticated for the model
- `MERIDIAN_PI_BINARY` unset (default PATH resolution)

Expect:
- Meridian selects that binary, or fails before launch with explicit runtime/auth diagnostics
- Launch diagnostics include runtime kind (`path` or `override`), binary path, version,
  session dir, and `pi_runtime_auth_policy: shared-pi-agent-dir`
- `PI_CODING_AGENT_DIR` points at `~/.pi/agent` (Pi-owned auth tree)

## S16: Missing or incompatible installed runtime fails fast

```bash
PATH=/empty meridian --harness pi -m <pi-model>
MERIDIAN_PI_BINARY=/bad/pi meridian spawn --harness pi -m <pi-model> -p "hi"
```

Expect:
- Empty `PATH` fails before launch with `Pi is not installed or not on PATH` guidance
- `/bad/pi` fails before launch with incompatible-binary guidance (`pi update` when the
  file exists but lacks required flags)
- Neither case starts a bundled or fake Pi runtime fallback

## S17: Spawn show exposes Pi phase

```bash
uv run meridian spawn --harness pi -m <pi-model> --bg -p "Reply OK and run no commands."
meridian spawn show <spawn-id> --verbose
```

Expect:
- `meridian spawn show --verbose` output includes `Pi phase:` while running or after completion
- A hang is diagnosable as one named phase, for example
  `waiting_for_first_pi_event_after_prompt`,
  `waiting_for_continuation_completion`, `pi_notification_timeout`,
  `semantic_completion_recorded`, `cleanup_stop_sent`, or `cleanup_failed`

## S18-S19: Runtime and phase diagnostics

Run one successful primary launch and one successful spawned RPC launch with default
`PATH` resolution, then again with `MERIDIAN_PI_BINARY` set to the **same** real `pi`
binary (non-PATH install smoke).

Expect:
- Diagnostics identify `path` vs `override` runtime kind (`PATH` vs `MERIDIAN_PI_BINARY`)
- Auth/config remains Pi-owned; `PI_CODING_AGENT_DIR` remains the standard agent tree and Meridian does not set a fake auth root
- Spawned `spawn show --verbose` exposes a useful Pi phase during startup and cleanup

## S20-S22: Sidecar lifecycle ingestion and stderr isolation

Run a normal spawned RPC scenario (e.g. S6) with real `pi` and Meridian extensions.

Inspect `<runtime-root>/spawns/<spawn-id>/pi-lifecycle-events.jsonl` (Meridian sets
`MERIDIAN_PI_LIFECYCLE_EVENT_FILE` for the child process).

Expect:
- Spawned RPC ingests canonical lifecycle lines from the sidecar file
- Incidental Pi stderr stays in spawn stderr logs and does not drive quiescence by itself
- Duplicate lifecycle lines are deduped in history (see spawn history / unit tests for
  edge cases)

## S23-S28: Child-wave batching and timeout behavior

Use real prompts that start multiple tracked/detached children (combine S3, S4, S8
patterns in one session), or rely on automated coverage in
`tests/unit/streaming/test_pi_quiescence.py` for mixed-wave and timeout edge cases.

Expect when exercising manually:
- One aggregate notification per child wave when multiple tracked jobs complete together
- Detached children do not block quiescence
- Tracked timeouts finalize the spawn without waiting on detached jobs

## S29-S32: Primary lifecycle extension remains non-invasive

```bash
uv run meridian --harness pi -m <pi-model>
```

From the native TUI, start `meridian spawn` child work and wait for completion.

Expect:
- Primary argv loads meridian-spawn-watch only (not background-tasks), `--mode rpc`, or `--no-extensions`
- Child completion can trigger a native Pi follow-up/notification
- Primary does not auto-finalize or exit at quiescence; user remains in the TUI
- Lifecycle events are written to `pi-lifecycle-events.jsonl` sidecar, not stdio
- Raw lifecycle JSON (`parent_spawn_id`, `correlation_id`, `emitted_at_ms`,
  `meridian.notification.*`, etc.) does **not** appear in the visible TUI
- Primary lifecycle diagnostics, if captured, are diagnostic-only and do not drive spawned quiescence

## S33-S36: Primary Pi session identity and continue/fork

1. Run primary Pi and send at least one prompt that causes Pi to persist a session.
2. Exit the TUI.
3. Inspect `meridian spawn show <primary-spawn-id>` and primary metadata.
4. Run:

```bash
uv run meridian --continue <chat-id-from-primary>
uv run meridian --fork <chat-id-from-primary>
```

Expect:
- Meridian records the actual Pi session UUID from flat
  `PI_CODING_AGENT_SESSION_DIR/*.jsonl` files, matched by cwd and launch time
- `--continue` projects to native `pi --session <uuid>`
- `--fork` projects to native `pi --fork <uuid>`
- No cwd-bucketed Pi session layout is assumed
- If Pi created no session, continue fails with a "no Pi session was created" diagnostic
- If session files exist but cannot be matched/parsed, continue fails with a distinct discovery diagnostic
