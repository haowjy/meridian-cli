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

## Adding a Harness

Follow [`docs/harness-integration.md`](../../../../docs/harness-integration.md) and touch
every file in `HARNESS_EXTENSION_TOUCHPOINTS`.

## Depth

[.context/CONTEXT.md](.context/CONTEXT.md) holds contracts, approval routing and the
terminal event table. See also [adapter-notes.md](.context/adapter-notes.md),
[session-transcripts.md](.context/session-transcripts.md) and
[pi-integration.md](.context/pi-integration.md).
