# pi_runtime/

Meridian-owned TypeScript extensions that run inside Pi. This directory owns the
extension seam for spawned RPC sessions and primary native TUI sessions; it does
not package or control the Pi runtime itself.

## Mental Model

Extensions write durable coordination state and the Python streaming layer
observes it. Keep stdout reserved for Pi JSON-RPC: do not add a sidecar event
transport or make command-line parsing the source of spawn authority.

`managed-bash` owns shell-task execution and task records.
`meridian-spawn-watch` owns child-spawn observation and follow-up notifications.
Keep that mechanism/policy boundary intact.

## Key Rules

- Use the shared JSON-file helpers for disk state; readers can encounter a
  missing or truncated file.
- Correlate spawned work through `MERIDIAN_PI_BASH_ID`, persisted spawn rows,
  and the origin sidecar — never argv parsing.
- Change extension source, never `dist/` output. Rebuild bundles before testing
  source changes; launch projection may otherwise use an installed bundle.
- Import Pi packages only from their package roots; extension-loader subpath
  imports are not reliable.

## Depth

- [.context/CONTEXT.md](.context/CONTEXT.md) — contracts, build/projection
  flow, disk-state boundary, and rationale
- [README.md](README.md) — contributor build and verification commands

## Related

- [../lib/streaming/.context/CONTEXT.md](../lib/streaming/.context/CONTEXT.md)
  — Pi drain and quiescence consumer
- [../lib/harness/.context/CONTEXT.md](../lib/harness/.context/CONTEXT.md)
  — Pi adapter and runtime resolution
