# chat/normalization/ — HarnessEvent → ChatEvent Translation

Layer 2 of the chat pipeline. Takes raw `HarnessEvent` objects from the drain loop and emits `list[ChatEvent]` for persistence. One normalizer class per harness.

## Mental Model

The drain loop produces harness-specific raw events. Normalizers translate them into harness-agnostic `ChatEvent`s. The translation is one-to-many (one `HarnessEvent` may produce zero, one, or several `ChatEvent`s). Normalizers are stateful per execution — they track turn state, deduplicate completions, and emit synthetic events at boundaries.

```
HarnessEvent (drain loop)
    └── ChatEventObserver (chat/event_observer.py)
          └── per-harness Normalizer.normalize()
                └── list[ChatEvent] → ChatEventPipeline (persist → index → fan-out)
```

## Key Rules

- **One normalizer class per harness.** Register it in `registry.py` via `get_normalizer_factory(harness_id)`.
- **Normalizers are stateless across events but stateful per execution.** `reset()` is called at execution boundaries (reconnect, reacquire). Don't assume state from a previous execution carries over.
- **The harness layer must not import from `chat/normalization/`.** Import direction is one-way: normalization imports from harness types, not the reverse.
- **One `HarnessEvent` may emit zero or more `ChatEvent`s.** A normalizer returning an empty list is valid — some raw events have no meaningful chat projection.

## Entry Points

- `registry.py` — `get_normalizer_factory(harness_id)`: look up a normalizer class by harness; raises if not registered
- `base.py` — `EventNormalizer` protocol: `normalize(event) → list[ChatEvent]` and `reset()`
- `claude.py`, `codex.py`, `opencode.py` — per-harness normalizer implementations
- `common.py` — shared helpers: `canonical_item_type()`, `extract_files()`, `as_dict()`, `as_str()`
- `synthetic.py` — `is_turn_boundary_event()`: adapter for synthetic `meridian/turn_completed` events

## Adding a Normalizer (New Harness)

1. Create `<harness>.py` implementing `EventNormalizer`
2. Register in `registry.py`
3. Do not retain state across `reset()` calls

→ [.context/CONTEXT.md](.context/CONTEXT.md) — normalizer contracts, stateful turn tracking, completion dedupe rule, per-harness event mappings, import direction
→ [../.context/CONTEXT.md](../.context/CONTEXT.md) — five-layer model; normalization is layer 2
→ [KB: architecture/chat/normalization.md](/home/jimyao/.meridian/git/meridian-flow-docs/kb/architecture/chat/normalization.md)
