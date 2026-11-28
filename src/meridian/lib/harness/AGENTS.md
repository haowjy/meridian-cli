# Harness adapters

A restriction is lost when a harness can't express part of a profile. A lost
restriction that changes what an agent can do is shown by default, in Mars's output and
in `--dry-run`, not at launch; every other lost field shows under `--verbose`. Nothing is
dropped without a record.

OpenCode 1.x is legacy and frozen; new OpenCode work targets V2. Transcript and storage
reads pick their dialect by DB schema, never by the installed binary or an env var.

## Bootstrap

Importing a submodule must not load the adapters (a lint bars module-level bootstrap
calls): a thread importing `adapter` while another loads `claude` can see a
half-initialized module. A `SpawnParams` field an adapter ignores must be listed in
`explicitly_ignored_fields`; bootstrap fails startup otherwise.

## Rules

- **Terminal events are harness-aware.** `event_type` is not globally unique, so qualify
  it by `event.harness_id`. Child Codex threads and OpenCode task sessions reach
  subscribers but never complete or fail the parent (`connection.primary_event_scope`).
  Shared `semantics.py` holds no harness event names, and a new harness adds no cases to it.
- **Native identity is planned, not discovered.** A chat binds one immutable
  `(harness, native_store, id)`. The source store comes from the recorded chat, never
  config hints. Cwd, timestamps, logs and newest-file or prefix scans never establish
  chat identity. `observe_after_exit()` never binds or persists; `launch/native_run.py`
  owns ordering and attribution. `legacy_native_stores.py` is a one-time import, not a
  runtime fallback.
- **Transcript resolution is exact**: recorded store plus full native ID, and several
  matches fail. A recorded Claude project store is never a config root. A missing file is searched for by exact ID, within the recorded config
  root, only on a tracked `--continue`. Recorded paths change only through
  `meridian session relocate`.
- **OpenCode's prompt, permissions, model and workspace grants ride
  `OPENCODE_CONFIG_CONTENT`**, built once at bind; connections never rewrite it. Every
  surface that opens or resumes a session must select the reserved policy agent, or the
  session runs without Meridian's permissions.
- **`env_defaults()` fills missing or blank child values** from the launch context and
  effective child env, never ambient env. Forced adapter policy rides
  `project_launch_env()`.
- **Spawn-owned journals** write through the published-spawn artifact seam, so a late write
  can't recreate a deleted spawn directory.
- **Pi usage is per assistant message**: fold each `message_end` once, because
  `agent_end.messages` repeats history.
- **Launch finalization owns `report.md` and `stderr.log`.** Harnesses expose evidence and
  attempt facts, not a second per-spawn file store.
- **`model_observation.observe_session_model` is the one writer of model observations.**
- Native idle parsing stays in `*_idle.py`; `lib/idle/` owns policy.

## `agy/` is hosted by its plugin

Meridian never runs Antigravity. `plugin_hosted.PLUGIN_HOSTED` is the one list of such harnesses;
`HarnessId.launched_by_meridian` derives from it. Because Meridian may not exec it, a launch that
names it **prints** the command (`meridian agy`, `--harness agy`, `--continue <agy chat>`);
shortcut names, `--harness` help and streaming serve ask this. The harness's plugin writes the session's row and no Meridian runner exists, so
readers of stored rows ask it, never `== "agy"`. It makes `spawn cancel` refuse (a server
holds every conversation of one `agy` process, and nothing Meridian may signal stops
`agy`), keeps `spawn` off the harness (`_excluded_harnesses_for_surface` adds it to Mars's
caller exclusions), words a busy chat as "open in agy" (no fork or message advice), makes
`decide_reentry` offer a resume only when the chat is not open, and keeps `session repair`,
the diag orphan capture and legacy import away from it.

A printed launch is decided once, at startup (`launch_prints` in `lib/launch/print_only.py`:
the pinned harness, or the harness of the chat `--continue` resolves read-only), and
`GlobalOptions.prints_launch` carries it: startup treats it like `--dry-run` (read-only
bootstrap, no background repairs, autosync or telemetry maintenance), and `run_primary_launch`
runs it as a dry run and returns `PrimaryLaunchOutput.printed` (`printed.py`). Mars may name a
harness Meridian may not exec only for a pin (`bundle_adapter._parse_harness_id`); any other
launch naming one is an error. Everything the command needs reaches it through the adapter:
`project_launch_env` is the printed environment overlay (`_MERIDIAN_AGY_AGENT`, `_MERIDIAN_AGY_PROJECT`, approval),
`project_content` drops Meridian's guidance (the hook delivers it), and workspace roots come
from `SpawnParams.workspace_roots` (the spec's `AgyLaunchSpec.workspace_roots`: projected roots
minus Meridian's runtime, temp and socket roots and the control root). What printing needs of a
harness (options it cannot honour and why, the `--work`/`--task-dir` note, where scripts spill)
is its `LaunchPorts.printed` (`PrintedLaunchPorts`), required exactly for plugin-hosted harnesses;
`print_only` and the CLI ask the ports and name no harness. A root holding `.agy/`
or `.agents/` is not added (a warning says so). The hook copies the overlay into the request
file and `holds.py` writes it, with the model, into the row's `LaunchPolicySnapshot` and
model selection, which is what a continue replays; a row without them still continues with `-m`.

`agy/permissions.py` turns the bundle's uncovered `deny`/`ask` rules that carry `native_permission`
(Mars's `agy` permission strings; read as given, no shape filter) into one inline
`--settings '{"permissions":...}'`, compiled once per launch in `bind_agy_spec` (argv and the
report read the result) on launch and `--continue`. `agy` uses the last `--settings` and
`--settings` replaces a list, so ours are added to the list agy would have used: the person's
`--settings` (inline JSON or file path; its other keys stay) if it has that list, else their
`settings.json`. Lists we add nothing to (`allow` always) are left as agy reads them; don't union
them, or a person's own `--settings` stops overriding their `settings.json`. The person's
`--settings` leaves `extra_args`. An unreadable source adds a report warning and the launch goes on
(an unreadable `--settings` is passed as given, without ours). It never writes `allow`, and leaves `ask`
out under `approval: never`. A continue has the rules because the plugin's snapshot has
`tool_rules_recorded=False` (nothing recorded, as against recorded-none): replay asks Mars with `--agent` and no
`--agent-tool-rules`, so the agent's current profile rules apply.

`agy` registers launch ports only (`register_harness(LaunchPorts(...))` in `agy/adapter.py`);
`register_harness` requires process ports exactly when the harness is not plugin-hosted.
Process code (runners, `primary_attach`, `primary_hold`, streaming serve and dispatch,
connections) asks `get_process_ports`, which raises `KeyError` for `agy`; code that builds a
launch asks `get_launch_ports`. `AgyAdapter` extends `AgyReader` (`agy/transcript.py`), so the
registry's adapter serves `session log`, search, capture and `--from` like any other harness;
`registry.ids()` and `contracts()` list only harnesses Meridian may exec. Readers of a stored
key go through `get_reader`, never `get_subprocess_harness`. An `agy` chat records no native
store: its layout names it (`plugin_hosted.native_store`, used by `NativeKeyFields.complete()`),
so the chat record is unchanged and older builds read it as before. Caveat: that store is
computed from the reading process's `HOME` (one field, two sources: recorded for launched
harnesses, computed for plugin-hosted ones), so a move of Google's store changes every `agy`
key. `agy` has no capture dialect, so capture reports it unsupported.
Outside `lib/harness/`, reach these harnesses only through `plugin_hosted.py` (argv dispatch,
the owed-notice sweep, store); a second one is a package with `commands.run_command`,
`notices.send_owed_notices` and `state.native_store`, an adapter, and a line in `_PACKAGES`.

`agy/transcript.py`: `transcript_full.jsonl` is the file (`transcript.jsonl` truncates
  fields). The parser skips and flags what it doesn't recognise; Google documents no format
  and it changes with `agy` versions, so check `agy --version` against the module docstring.

`agy/` also holds what the plugin runs:

- `harness-hook agy` (`hook.py`; events `SessionStart`, `PreInvocation`, `Stop`): dispatched from `cli/entrypoint.py`, so import nothing
  heavy at module level. No blocking locks (registering a request file waits at most 0.5 s for the
  conversation's lock, the one the notice sweep decides closed-ness under), no output for
  no opinion, catch everything. It writes request files, `briefed` and `seen` markers and, at
  `Stop`, the idle-wake marks below (`asked`, `backoff`, removing `busy`); it claims inbox
  entries at `PreInvocation` and confirms them at `Stop`. The per-call path never reads the
  whole transcript. Its guidance comes from the launch builders in `guidance.py` (local
  imports, about 0.4 s, so only the call that registers a conversation, the one `PreInvocation`
  after the row appears and the first one after a compaction pay it); never fork that text. The row file the server writes is the signal that
  the session's facts can be injected, and `guard.sh` lets the hook run for it until the
  `briefed` marker exists. Whether the model's context holds a block is read from the
  transcript (`context.py`: the steps after the last `CHECKPOINT`), never from "this
  process injected it": a resume starts a new process over a context that has the blocks,
  and a compaction empties the context of a process that injected them. `agy` has no
  compaction hook; `guard.sh` greps the transcript bytes after the `seen` offset for
  `CHECKPOINT`.
- Idle wake (`watch.py`; the `Stop` hook and `guard.sh` are its other halves). `agy` wakes the
  agent when a background command it started exits, so the agent keeps `harness-watch agy`
  running. The watcher imports only `state.py`, reads state and writes none: it opens
  `watch/<conversation>.lock` (which `Stop` creates) and holds its `flock` while it lives. It
  exits, waking the agent, when the session is not busy and the inbox changed since it
  started (keepwarm pings and the state reminder arrive as inbox messages). It exits silently
  when its parent shell or the `agy` process is gone.
  `guard.sh` creates `busy/<conversation>` on every
  `PreInvocation`. At `Stop`, when no watcher holds the lock, the hook asks the model once
  per turn (`asked`) to start one; it never asks for a subagent or a cancelled turn. When it
  doesn't ask, it removes `busy` and `asked`. After two asks in a row end with no watcher, it
  skips 2 turn ends before asking again, then 4, 8, up to 32 (`backoff/<conversation>`); a
  `Stop` that sees a watcher running deletes that back-off file.
  agy 1.5.15 fires no `Stop` on Esc, so a cancelled turn leaves the `busy` file, and after any
  Esc agy makes no model call for a finished background command until the person types: a
  message after Esc waits, and the watcher waits on `busy` alone (it stays running). Only the
  idle sensor reads the cancel, through `agy/idle.py` `TurnState`: a new `executor_metadata`
  row with termination reason 2 (`USER_CANCELED`) in the conversation's SQLite database after
  `busy` was last touched, against the highest row index noted at that touch; until that
  read succeeds the turn counts as running. That schema is undocumented (seen on 1.5.15);
  anything unreadable leaves the turn running. An Esc within about a second of any model
  invocation can be missed (no idle notice until the turn ends); a build that renumbers the
  enum could read a mid-turn execution end as an Esc. agy's terminal sandbox (Auto mode) has
  its own process list and hides `~/.meridian`,
  so the start command says `BypassSandbox true`, and a watcher that cannot see its state
  prints a notice that it is sandboxed and exits. These files live in the `agy` process
  directory, so the notice sweep's removal of it is their only cleanup. The message itself
  still arrives through `PreInvocation`.
- `harness-mcp agy` (`mcp_server.py`, `holds.py`): the only writer of Meridian state for
  `agy`. It holds one row per primary conversation through `launch/primary_hold.py`, with
  no runner PID, so the launch lock is the row's liveness. Its stdout is the MCP transport:
  logging goes to stderr, and each command runs with piped stdio and the identity of
  the calling conversation's row, never the `MERIDIAN_*` session names the server inherited
  from the shell that started `agy` (`call_env`, below).
  Each hold also runs the idle sidecar (`lib/launch/hold_idle.py`: closes the conversation's
  old stretch, then `sidecar.run`) with `AgyIdleSensor` (`lib/harness/agy/idle.py`). The
  sensor asks `TurnState.running()` (same file) whether the turn is live: the `busy` file, plus
  the cancel read above. On each turn end the sensor reads the transcript past an
  in-memory offset (kept on the sensor, not persisted; a transcript that shrank is re-read).
  A step with `type` `USER_INPUT` and `source` `USER_EXPLICIT` is a typed prompt: a return,
  with its `created_at` as `returned_at_ms`. A wake's `SYSTEM_MESSAGE` (and hook-injected
  `SYSTEM_SDK` steps) is not. An unreadable transcript reports nothing and is retried next poll.
  `IdleState.last_turn_id` is the step index of the last typed step the stored stretch saw.
  `hold_idle.py` reads it before closing the old stretch because the sidecar's first arm (an
  identity pin that opens a stretch with no return) overwrites it with none; the sensor
  treats a typed step equal to it as already reported. A re-adoption in the same server
  process (a failed hold retried by the poll) is not a reopen and does not close the stretch.
  The sensor is also the sidecar's `PromptSink`: when `fire warn` acts, the sidecar calls
  `AgyIdleSensor.send("reminder" | "ping", stop_offered=..., expires_at=...)`, which queues
  `prompt_text(...)` through `inbox.enqueue` with no sender (so no notice is owed if it is never
  shown) and an `expires_at` (the cache-cold time: `claim` and `confirm` drop an expired entry
  instead of injecting or re-queueing it, and its render has no peer-message framing), but only
  when a wake is possible (`_can_wake`: the watcher runs, no `busy` mark exists, and no turn end
  is unreported; the watcher never exits for a running turn or an Esc's leftover mark): otherwise
  the entry would wait for the person's next prompt and tell them they are away, so `send`
  returns False, the sidecar returns the slot, and a dropped reminder stays owed
  (`Stretch.reminder_owed`) for the next turn end. The sidecar spends a slot before `send` and
  leaves it spent if the send is cancelled. The wording mirrors the Claude mod's without `compact-now` (`agy` cannot compact on
  demand); the wake notice, not the prompt, asks for the watcher restart. Keepwarm's later pings are timed by the sidecar
  (`idle/keepwarm.py`; in-memory record, none after a restart), one lead after
  each non-return turn end. The sensor remembers the ids it sent; a turn whose steps carry one
  is reported with `IdleEvent.prompt_replies`, from which core reads `stop-waiting`. The ping turn
  is not kept read-only: `PreToolUse` carries no read-only flag, a known gap.
  A typed `/compact` (its summary is a model call that ends like a turn) is reported as a
  `user_prompt` event, which closes the stretch and arms none; `/clear` writes nothing to the
  old conversation and starts another, so the sensor reads the TUI's `flock` on
  `~/.gemini/antigravity-cli/presence/<conversation>.lock` (dropped on `/clear` and on exit; a
  missing file or failed probe counts as held), reports `user_prompt` once when it is gone, and reports
  no turn end until it is held again. Without that, the old conversation's watcher keeps
  waking it for pings no one sees.
  Config reaches the sidecar through `holds._idle_env`: `call_env(row)`, which is the server's
  environment through `env_registry.session_free_env` (injected handles and hook payload
  dropped, config inputs kept) plus the row's identity (its spawn ID, chat ID, project root
  and runtime root), and `_MERIDIAN_HARNESS=agy`; the result is
  `SidecarContext.env`, which `IdleService` and notice delivery read. agy has a built-in TTL of 1800 s (`harness_models.py` `idle_defaults`, `idle/deadline.py`; Google publishes no window; the cache was observed to hold across 25-minute idle gaps, seen 23 Nov 2026), so the warning lands 20 min after a turn at the global lead. `holds._run_loop`
  runs each hold's coroutine on its own `new_event_loop()` and closes it without joining the
  default executor (`asyncio.run` would): a notice mid-send in a `to_thread` worker lasts up to
  10 s, and `primary.complete` must run within the ~400 ms agy gives the server. A serving
  failure completes the row as failed.
- `inbox.py`, `notices.py`: a queued message (`DeliveryKind.QUEUED`) is injected at the next
  `PreInvocation` (`inbox.claim` moves it to in-flight and notes the transcript's size) and
  confirmed at the turn's `Stop` (`inbox.confirm` looks for its `[meridian:<id>]` marker in the
  transcript past the earliest noted size, which `agy` writes when it injects, before the model
  call, so an Esc'd turn's marker is there too). An entry without its marker goes back to the
  inbox, which wakes an idle session; a turn with no `Stop` leaves its batch in flight for the
  next one. Confirm runs before the watcher ask, since an ask's `continue` reply must not defer
  it. A message injected three times without showing stays in flight, uninjected, and is owed
  a notice; `settle_undelivered` checks both files against the whole transcript. An in-flight
  entry an older build wrote has no size and is read against the whole file. `guard.sh` starts
  Python for `PreInvocation` only for queued messages, never for in-flight ones. The sender
  arrives as data (`MessageSender`, from `meridian message` through the route), never parsed
  from the text. Nothing is sent at exit (the server has about 400 ms before SIGKILL);
  `send_owed_notices` runs at the next server start and in primary-launch recovery, from the
  sender's project, removing each entry only after its notice was attempted. A tool call
  with no held row returns an error and never runs anonymously.

The shell guard and Mars declarations are in `src/meridian/mars/`; the guard hard-codes the
paths in `agy/state.py`.

## Adding a Harness

Follow [`docs/harness-integration.md`](../../../../docs/harness-integration.md) and touch
every file in `HARNESS_EXTENSION_TOUCHPOINTS`.

## Depth

[.context/CONTEXT.md](.context/CONTEXT.md) holds contracts, approval routing and the
terminal event table. See also [adapter-notes.md](.context/adapter-notes.md),
[session-transcripts.md](.context/session-transcripts.md) and
[pi-integration.md](.context/pi-integration.md).
