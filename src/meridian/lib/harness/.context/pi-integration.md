# lib/harness/ — Pi Integration

Pi is the only harness with Meridian-owned in-process extensions and quiescence-based
completion. This page holds its adapter integration context; shared adapter contracts
remain in [CONTEXT.md](CONTEXT.md).

## Extension Architecture

Meridian loads three TypeScript extensions inside Pi:

- **managed-bash** — shell execution, `bash` / `bash_manage`, `/ps*` UI, and durable task records.
- **meridian-spawn-watch** — canonical direct-child observation, `/spawn*` UI, and idle-turn result delivery.
- **session-boundary** — bounded native lifecycle observations for post-exit identity verification.

Both primary and spawned launches load managed-bash and spawn-watch when their
`[harness.pi]` toggles are enabled. Session-boundary is always loaded, with no
config toggle. Primary keeps the native TUI and does not auto-stop on quiescence.

Shared helpers under `src/meridian/pi_runtime/extensions/shared/` provide schemas,
validated receipt/reservation readers, atomic JSON, paths and UI. Python's matching
models live in `pi_private_state.py`. The coordination boundary is
the disk state the extensions write and the Python side observes.

Extensions are built with `pnpm run build:extensions`. Projection prefers the
package's `pi_runtime/dist/extensions/<name>/index.js`, falling back to
`pi_paths.resolve_meridian_pi_extension_root()` (`~/.meridian/pi/extensions/`).
These are stable `-e` paths, not per-launch copies.

Bundle toggles and `load_all_pi_extensions` resolve from the launch config snapshot
in `bind_launch_context()` → `SpawnParams.pi_harness_profile` →
`PiAdapter.resolve_launch_spec()`, not ambient CWD config reload. Spawned RPC
suppresses ambient extensions by default; `load_all_pi_extensions = true` retains
native discovery and adds configured extra roots. Primary always retains native
discovery. Both roles reject passthrough mode/extension selectors.

The runtime itself is resolved by `pi_runtime_resolver.py`. Both managed TUI primary
and spawned RPC roles require a stable Pi `>=1.1.0 <2`; prerelease, ambiguous or
unparseable output and newer majors fail closed before provider work. The resolver also
probes the installed binary's role-specific `--help` surface (required tokens differ
between primary and spawned roles) and returns a `PiRuntimeResolution`. The managed
extension peer range matches that contract; its development SDK is pinned to the
qualified Pi `1.1.0`.

## Native Identity

The adapter resolves the child session directory during launch binding, not
prelaunch or RPC startup. Primary creates use the flat Meridian Pi session root;
spawn creates use its spawn-scoped child directory. Resume retains its recorded
store. Both environment and explicit `--session-dir` come from that same `NativeIdentity`.

Create and fork mint IDs only after the store is known, checking every local
journal's first-line header for collisions. Resume/fork source selection requires
one exact filename suffix and a matching session header, and passes an absolute
path to Pi. Missing/empty files cannot be resumed: Pi would silently create a new
identity there. Post-exit verification checks only the assigned entry (including
the fork parent); an unmaterialized create stays bound and pending. It never
selects a newest/cwd-matching journal. Meridian writes no Pi native journal.

`prepare_prelaunch()` gives session-boundary a path and launch nonce. The extension
writes at most 16 KiB to `spawns/<run>/pi-session-boundary.json`, using its own PID.
`observe_after_exit()` first verifies the assigned entry, then reads the v2 record
with the expected nonce and actual Pi PID. Only a final
`session_shutdown(reason=quit)` with readable native identity supplies an exit.
Entry/last-seen identity, switches, reloads, stale-context shutdowns, and missing or
invalid records cannot substitute for a verified quit. The launch layer owns exit
allocation and persistence. See the
[native boundary contract](../../../pi_runtime/.context/CONTEXT.md).

## Completion and Disk State

Pi spawned sessions do not exit on task completion — they stay alive to track child
spawns and deliver wave notifications. Completion is gated on **quiescence**: the parent
agent is idle, all reconciled transitive descendants and Pi-private work have finished,
and all pending notifications have been delivered and acknowledged. The Python drain loop
delegates Pi-specific completion policy to `lib/streaming/pi_drain.py:PiDrainCoordinator`.
Persisted descendants come from shared reconciled evidence; `PiQuiescenceTracker` and
`PiDiskWatcher` supply private bash/notification evidence. `SpawnManager` remains generic;
Pi child-wave, notification, micro-drain, and cleanup decisions stay behind the
coordinator boundary.

Pi extensions coordinate through disk files, not a separate lifecycle transport:

- child spawn records under `runtime_root/spawns/<child>/state.json`
- bash state under `runtime_root/pi-bash/<parent>/bash-records.json`
- exact custom-message admission receipts and Python public-event observations under
  `runtime_root/pi-bash/<parent>/delivery-receipts.json` and `delivery-observations.json`
- explicit result consumption and supervised delivery faults in the same parent directory

The retired timestamp marker is ignored. See the
[delivery contract](../../../pi_runtime/.context/delivery-contract.md) for schemas and
restart limits. A queued message is not admission; an unobserved admitted message fails
closed after bounded recovery, with its original evidence reason.

`ReconciledDescendantEvidence` owns persisted-descendant authority.
`meridian-spawn-watch` owns extension-side observation and notification; `PiDiskWatcher`
consumes only the private bash and notification files. If a spawn lifecycle event appears
on stdout, it is diagnostic noise, not persisted-descendant authority.

## Legacy 0.6.7 sessions

0.6.7 headless spawns wrote Pi journals under `<pi root>/<spawn-id>/` (primaries
used the shared root) but never recorded the session ID. The evidence helpers in
`legacy_native_stores.py` validate a candidate the same way as live reads (header
`type: session` with an `id`, version 1 to 3, exact `*_<id>.jsonl` resolution) and
read its cwd, header time, first user message and final assistant text. What
counts as proof is decided in `ops/legacy_native_import.py`, not here. 0.6.7 ran
Pi in the control root, so header cwds often equal `control_root`, not the
recorded worktree `execution_cwd`.

## Related Context

- [CONTEXT.md](CONTEXT.md) — shared harness architecture and adapter contracts
- [../../streaming/.context/pi-drain.md](../../streaming/.context/pi-drain.md) — Pi quiescence drain and cleanup policy
- [../../../pi_runtime/.context/CONTEXT.md](../../../pi_runtime/.context/CONTEXT.md) — TypeScript extension contracts and build pipeline
