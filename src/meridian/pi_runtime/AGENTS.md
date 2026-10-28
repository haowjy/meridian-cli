# pi_runtime/

Meridian-owned TypeScript extensions that run inside Pi. This directory owns the
extension seam for spawned RPC sessions and primary native TUI sessions; it does
not package or control the Pi runtime itself.

## Mental Model

Extensions write durable coordination state and the Python streaming layer
observes it. Keep stdout reserved for Pi JSON-RPC: do not add a sidecar event
transport or make command-line parsing the source of spawn authority.

One extension per concern; keep the mechanism/policy boundary (extensions observe and
translate, Python core decides):

- `managed-bash` — shell-task execution and records; reload rebinds hooks without killing
  live tasks, and cold recovery never signals a persisted PID.
- `meridian-spawn-watch` — child-spawn observation and result notifications; arms idle
  delivery only at a main-loop `agent_end`, never at session start.
- `session-boundary` — bounded native lifecycle observations; only a final shutdown/quit
  qualifies run exit. Depends on Pi's exact error-text prefix (see README).
- `meridian-idle` — primary-only; turns native idle/input events into `meridian idle`
  commands and never classifies Meridian commands itself.
- `meridian-permissions` — launch-local scoped rules and `ask`, no disk authority; separate
  from managed-bash. A present but unusable policy must throw at load (Pi treats that as a
  fatal launch error); unsupported shell syntax is refused when the policy says
  `bash_fail_closed`.

Per-extension detail: [.context/CONTEXT.md](.context/CONTEXT.md).

## Key Rules

- Use the shared JSON-file helpers for disk state; readers can encounter a
  missing or truncated file.
- Python readers union Pi result consumption through
  `lib/harness/pi_private_state.py`: current and legacy CLI observations from wait,
  ack, terminal reports observed by show/status/report-show, and cancelled results
  recorded under their caller, exact delivery receipts, and explicit clears. Pi's
  human-facing show/wait/cancel commands pass `--no-observe` so model delivery stays
  owed. A damaged source is skipped and diagnosed
  independently, never treated as a reason for no-arg wait to fail.
- Scope children by canonical `parent_id` and deliver only a background `launch_mode`
  (core's `is_owed_child`, mirrored as `isBackgroundLaunch`); transfer launcher work
  through a background row's `originating_bash_id`. The parent set is
  core's, passed at launch as `_MERIDIAN_PI_OWED_PARENT_IDS` for a primary (every
  primary generation of its chat) and absent for a spawned or nested session (its own
  children only); never re-derive generations here. Logs and origin sidecars are not
  authority.
- `sendMessage()` returns void; receipt authority is exact native custom-message
  admission. Keep automatic publication finite and bounded, preserve exact batch
  membership, and leave excluded or changing work owed. Candidate IDs and queued
  claims are mechanics, never lifecycle authority. See the canonical
  [delivery contract](.context/delivery-contract.md) for sequencing, bounds,
  revalidation, cadence, cancellation, and restart behavior.
- Change extension source, never `dist/` output. Rebuild bundles before testing
  source changes; launch projection may otherwise use an installed bundle.
- Import Pi packages only from their package roots; extension-loader subpath
  imports are not reliable.
- Tests wait with the shared `until()` in `extensions/shared/test_wait.ts`, which has no
  deadline of its own; vitest's `testTimeout` is the hang guard. Don't add a local poll
  helper with a short deadline, or a fixed sleep before an assertion
  (the repository root's `tests/AGENTS.md`, "No flaky tests").

## Depth

- [.context/CONTEXT.md](.context/CONTEXT.md) — contracts, build/projection
  flow, disk-state boundary, and rationale
- [.context/delivery-contract.md](.context/delivery-contract.md) — disk schemas,
  consumption/admission ownership and honest restart/crash semantics
- [README.md](README.md) — contributor build and verification commands

## Related

- [../lib/streaming/.context/CONTEXT.md](../lib/streaming/.context/CONTEXT.md)
  — Pi drain and quiescence consumer
- [../lib/harness/.context/CONTEXT.md](../lib/harness/.context/CONTEXT.md)
  — Pi adapter and runtime resolution
