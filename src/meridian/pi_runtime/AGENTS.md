# pi_runtime/

Meridian-owned TypeScript extensions that run inside Pi. This directory owns the
extension seam for spawned RPC sessions and primary native TUI sessions; it does
not package or control the Pi runtime itself.

## Mental Model

Extensions write durable coordination state and the Python streaming layer
observes it. Keep stdout reserved for Pi JSON-RPC: do not add a sidecar event
transport or make command-line parsing the source of spawn authority.

`managed-bash` owns shell-task execution and task records.
Its launch-scoped owner survives extension reload; reload rebinds hooks and does
not terminate live tasks. Cold recovery preserves records but never signals a
persisted PID: prior running rows remain tracked with `ownership_lost` until
explicit detach/manual recovery. Detach releases quiescence, not process
ownership; normal shutdown still cleans up owned groups. Publish terminal only
after group exit and queued output, and supervise every async task callback.
`meridian-spawn-watch` owns child-spawn observation and follow-up notifications.
Keep that mechanism/policy boundary intact.
`session-boundary` owns bounded native lifecycle observations; only a final
shutdown/quit qualifies the run exit, never the entry or last-seen session.
Its v2 record is nonce/PID-correlated and bounded. A shutdown with an invalidated
Pi context clears quit and stays unresolved. The stale-context exception depends
on Pi's exact error-text prefix; a changed prefix poisons the record fail-closed.
See README for that dependency.
`meridian-idle` is primary-only. It translates native idle/input events and live
context facts into `meridian idle` commands; the Python core owns every policy,
notification, persistence, and compaction decision.

## Key Rules

- Use the shared JSON-file helpers for disk state; readers can encounter a
  missing or truncated file.
- Scope children by canonical `parent_id`; transfer launcher work through the
  row's `originating_bash_id`. Logs and origin sidecars are not authority.
- `sendMessage()` returns void; receipt authority is exact native custom-message
  admission. Publish only while native `ctx.isIdle()`, recheck explicit consumption
  after formatting, and retain queued ownership across reload.
- Change extension source, never `dist/` output. Rebuild bundles before testing
  source changes; launch projection may otherwise use an installed bundle.
- Import Pi packages only from their package roots; extension-loader subpath
  imports are not reliable.

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
