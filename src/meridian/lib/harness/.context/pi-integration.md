# lib/harness/ — Pi Integration

Pi is the only harness with Meridian-owned in-process extensions and quiescence-based
completion. This page holds its adapter integration context; shared adapter contracts
remain in [CONTEXT.md](CONTEXT.md).

## Extension Architecture

Pi is the first harness with in-process TypeScript extensions rather than an opaque
subprocess. Meridian-owned extensions split by concern:

- **managed-bash** — task registry, `bash` / `bash_manage`, bash bridge, `/ps*` UI, and bash-record writes. See `src/meridian/pi_runtime/extensions/managed-bash/`.
- **meridian-spawn-watch** — spawn discovery, implicit-wait notification dispatch, `/spawn*` UI, and disk observation. See `src/meridian/pi_runtime/extensions/meridian-spawn-watch/`.

Shared helpers under `src/meridian/pi_runtime/extensions/shared/` are UI/path/schema/json/id
helpers only; they are not the runtime authority boundary. The coordination boundary is
the disk state the extensions write and the Python side observes.

Extensions are TypeScript, built with `pnpm run build:extensions`, and loaded via stable
`-e` paths from `pi_paths.resolve_meridian_pi_extension_root()` (`~/.meridian/pi/extensions/`
or packaged `dist/extensions`). `[harness.pi]` toggles and `load_all_pi_extensions` are
resolved from the launch config snapshot in `bind_launch_context()` → `SpawnParams.pi_harness_profile`
→ `PiAdapter.resolve_launch_spec()` (not ambient CWD config reload).

The runtime itself is resolved by `pi_runtime_resolver.py` — it probes the installed `pi`
binary for compatibility (required `--help` surface tokens differ between primary and
spawned roles) and returns a `PiRuntimeResolution`.

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
- notification marker under `runtime_root/pi-bash/<parent>/last-notification.json`

`ReconciledDescendantEvidence` owns persisted-descendant authority.
`meridian-spawn-watch` owns extension-side observation and notification; `PiDiskWatcher`
consumes only the private bash and notification files. If a spawn lifecycle event appears
on stdout, it is diagnostic noise, not persisted-descendant authority.

## Related Context

- [CONTEXT.md](CONTEXT.md) — shared harness architecture and adapter contracts
- [../../streaming/.context/pi-drain.md](../../streaming/.context/pi-drain.md) — Pi quiescence drain and cleanup policy
- [../../../pi_runtime/.context/CONTEXT.md](../../../pi_runtime/.context/CONTEXT.md) — TypeScript extension contracts and build pipeline
