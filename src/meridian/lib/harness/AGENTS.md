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
(Mars's `agy` permission strings; read as given, no shape filter) into an inline
`--settings '{"permissions":...}'` on launch and `--continue`. `--settings` replaces a list instead of merging,
so each list is unioned with the same list in agy's own `settings.json`; an unreadable file adds a report
warning and the launch goes on. It never writes `allow`, and leaves `ask` out under `approval: never`
(agy ignores it with `--dangerously-skip-permissions`).

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

- `harness-hook agy` (`hook.py`): dispatched from `cli/entrypoint.py`, so import nothing
  heavy at module level. No blocking locks (registering a request file waits at most 0.5 s for the
  conversation's lock, the one the notice sweep decides closed-ness under), no output for
  no opinion, catch everything. It writes only request files, `briefed` and `seen` markers, and
  claims inbox entries. Its guidance comes from the launch builders in `guidance.py` (local
  imports, about 0.4 s, so only the call that registers a conversation, the one `PreInvocation`
  after the row appears and the first one after a compaction pay it); never fork that text. The row file the server writes is the signal that
  the session's facts can be injected, and `guard.sh` lets the hook run for it until the
  `briefed` marker exists. Whether the model's context holds a block is read from the
  transcript (`context.py`: the steps after the last `CHECKPOINT`), never from "this
  process injected it": a resume starts a new process over a context that has the blocks,
  and a compaction empties the context of a process that injected them. `agy` has no
  compaction hook; `guard.sh` greps the transcript bytes after the `seen` offset for
  `CHECKPOINT`.
- `harness-mcp agy` (`mcp_server.py`, `holds.py`): the only writer of Meridian state for
  `agy`. It holds one row per primary conversation through `launch/primary_hold.py`, with
  no runner PID, so the launch lock is the row's liveness. Its stdout is the MCP transport:
  logging goes to stderr, and each command runs with piped stdio and the identity of
  the calling conversation's row, never the server's inherited `MERIDIAN_*`.
- `inbox.py`, `notices.py`: a queued message (`DeliveryKind.QUEUED`) is injected at the next
  `PreInvocation` and confirmed against `agy`'s transcript; a message injected three times
  without showing there stays in flight, uninjected, and is owed a notice. The sender
  arrives as data (`MessageSender`, from `spawn message` through the route), never parsed
  from the text. Nothing is sent at exit (the server has about 400 ms before SIGKILL);
  `send_owed_notices` runs at the next server start and in primary-launch recovery, from the
  sender's project, removing each entry only after its notice was attempted. A tool call
  with no held row returns an error and never runs anonymously.

The shell guard and Mars declarations are in `src/meridian/mars/`; the guard hard-codes the
paths in `agy/state.py` and `agy/inbox.py`.

## Adding a Harness

Follow [`docs/harness-integration.md`](../../../../docs/harness-integration.md) and touch
every file in `HARNESS_EXTENSION_TOUCHPOINTS`.

## Depth

[.context/CONTEXT.md](.context/CONTEXT.md) holds contracts, approval routing and the
terminal event table. See also [adapter-notes.md](.context/adapter-notes.md),
[session-transcripts.md](.context/session-transcripts.md) and
[pi-integration.md](.context/pi-integration.md).
