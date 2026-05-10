# chat/normalization/

Translates raw `HarnessEvent` objects into `list[ChatEvent]`. One normalizer
class per harness; registration maps harness ID to class.

## Entry Points

- `registry.py` — `get_normalizer_factory(harness_id)`: returns a normalizer class; raises if not registered
- `base.py` — `EventNormalizer` protocol: `normalize()` and `reset()`
- `claude.py`, `codex.py`, `opencode.py` — per-harness normalizer implementations

## Key Files

- `common.py` — shared projection helpers: `canonical_item_type()`, `extract_files()`, `as_dict()`, `as_str()`
- `synthetic.py` — `is_turn_boundary_event()`: adapter for synthetic `meridian/turn_completed` events

## Architecture

Normalizers sit between the drain loop and the persistence pipeline:

```
HarnessEvent (from drain loop)
        │
        ▼
ChatEventObserver (chat/event_observer.py)
        │
        ▼
per-harness Normalizer.normalize()
        │
        ▼
list[ChatEvent] → ChatEventPipeline (persist → index → fan-out)
```

Each normalizer is stateful per execution. `reset()` is called at execution
boundaries (reconnect, reacquire). The harness layer must not import from this
package.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — normalizer contracts, stateful turn tracking,
  completion dedupe rule, per-harness event mappings, import direction

## Related

→ [../.context/CONTEXT.md](../.context/CONTEXT.md) — five-layer model; normalization is layer 2
→ [KB: architecture/chat/normalization.md](/home/jimyao/.meridian/git/meridian-flow-docs/kb/architecture/chat/normalization.md)
