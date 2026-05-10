# lib/harness/ — Harness Adapters

Mechanism side of the policy/mechanism split. Translates `SpawnParams` into a
runnable subprocess or bidirectional connection, and extracts results back into
Meridian's domain types. Adding a harness means one adapter file and one
registry entry — no edits to shared code.

## Entry Points

- **`adapter.py`** — `SpawnParams`, `HarnessAdapter`, `SubprocessHarness`,
  `HarnessContract`, and all contract sub-models. The source-of-truth for what
  every adapter must implement.
- **`registry.py`** — `HarnessRegistry`, `with_defaults()`. The global singleton
  and the place to register a new adapter.
- **`claude.py` / `codex.py` / `opencode.py`** — concrete adapter implementations.
  Each declares its `HarnessContract` and `consumed_fields`/`explicitly_ignored_fields`.
- **`__init__.py`** — `HARNESS_EXTENSION_TOUCHPOINTS` and `ensure_bootstrap()`.
  Read this before adding a harness — lists every file that must be touched.
- **`common.py`** — shared helpers: `parse_json_stream_event()`,
  `extract_usage_from_artifacts()`, `extract_claude_report()` / `extract_codex_report()` /
  `extract_opencode_report()`, `extract_session_id_from_artifacts_with_patterns()`.
- **`semantics.py`** — `terminal_outcome()`, `activity_transition()`, `clears_signal()`.
  Cross-harness event classification used by the drain loop.

## Subpackages

- **`connections/`** — bidirectional connections (WebSocket, HTTP+SSE) for
  streaming and managed-primary paths. `base.py` defines `HarnessConnection`,
  `HarnessEvent`, `ConnectionCapabilities`.
- **`projections/`** — `project_<harness>_spec_to_cli_args()` functions. Each
  declares `_PROJECTED_FIELDS` and `_DELEGATED_FIELDS`; drift is caught at
  import time.
- **`extractors/`** — per-harness implementations of report, session-ID, and
  usage extraction from spawn artifacts.
- **`passthrough/`** — per-harness passthrough argument handling and registry.

## Supporting Files

| File | Purpose |
|---|---|
| `launch_spec.py` | `_enforce_spawn_params_accounting()` — drift guard for SpawnParams |
| `launch_types.py` | `SessionSeed` and other launch-phase types |
| `ids.py` | `HarnessId` enum and `TransportId` |
| `bundle.py` | `HarnessBundle` registration (transport map side effects) |
| `permission_broker.py` | Approval/sandbox permission flag resolution |
| `workspace_projection.py` | Cross-harness workspace root projection helpers |
| `session_detection.py` | Filesystem-based session ID detection (Claude primary) |
| `transcript.py` | Spawn transcript access helpers |
| `cost.py` | Cost normalization across harnesses |
| `errors.py` | Harness-layer error types |

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — contracts, translation pipeline,
   session-ID observation chain, how to add a harness, anti-patterns.

## Related

- `../launch/` — `build_launch_context()` and the four driving adapters that use
  harness adapters
- `../streaming/` — `SpawnManager` drain loop that consumes `HarnessEvent` streams
- `../state/` — artifact store that `SpawnExtractor` reads from
