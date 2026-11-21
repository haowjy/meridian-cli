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

## `agy/` is not a launched harness

Meridian never starts Antigravity, so `HarnessId.AGY` has no adapter, bundle or shortcut:
`launched_by_meridian` is false, and the registry's adapters are only harnesses Meridian launches.
Readers of stored rows ask `is_unlaunched_harness(name)`, never `== "agy"`: it makes
`spawn cancel` refuse (a server holds every conversation of one `agy` process, and nothing
Meridian may signal stops `agy`), `--continue`/`--fork` refuse, blocks resume in
`decide_reentry`, and skips legacy import. Reading is not launching: the registry's
`NativeStoreReader` (`registry.get_reader`/`find_reader`, the read half of an adapter, which
`SubprocessHarness` extends) serves `session log`, search, capture and `--from` for every
harness, and `agy/transcript.py` registers one for `agy`. Readers of a stored key go
through it, never `get_subprocess_harness`. An `agy` chat records no native store: its
layout names it (`unlaunched.native_store`, used by `NativeKeyFields.complete()`), so the
chat record is unchanged and older builds read it as before. Caveat: that store is
computed from the reading process's `HOME` (one field, two sources: recorded for launched
harnesses, computed for unlaunched ones), so a move of Google's store changes every `agy` key. `agy` has no capture dialect,
so capture reports it unsupported.
Outside `lib/harness/`, reach these harnesses only through `unlaunched.py` (argv dispatch,
the owed-notice sweep, store, reader); a second one is a package with `commands.run_command`,
`notices.send_owed_notices`, `state.native_store` and `transcript.READER` plus a line in
`_PACKAGES`, which `HarnessId.launched_by_meridian` also reads.

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
