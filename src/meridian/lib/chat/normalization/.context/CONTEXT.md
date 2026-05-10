# normalization — Contracts and Architecture

## Normalizer Contract

```python
class EventNormalizer(Protocol):
    def normalize(self, event: HarnessEvent) -> list[ChatEvent]:
        """Translate one HarnessEvent → zero or more ChatEvents. Empty list drops the event."""
    def reset(self) -> None:
        """Clear stateful context at execution boundaries (reconnect, reacquire)."""
```

Normalizers are **stateful per execution**. Each is constructed with `(chat_id, execution_id)` and
tracks turn state across the event stream for that execution. `reset()` is called — not the
constructor — when crossing an execution boundary within the same lifetime.

Adding harness support: one new file + one `NORMALIZER_REGISTRY` entry in `registry.py`.
`get_normalizer_factory()` raises `KeyError` for unregistered harness IDs.

## Turn Lifecycle Contract

The frontend expects exactly one `turn.started` and exactly one `turn.completed` per turn.
Normalizers are responsible for enforcing this invariant even when the harness emits partial,
duplicate, or synthetic events:

- Emit `turn.started` before any content or item events for a turn
- Emit exactly one `turn.completed` per turn; guard with `_completed_for_turn` state
- On `reset()`, clear all turn state — the next event can open a new turn

## Completion Dedupe

Synthetic `meridian/turn_completed` events close turns when the harness doesn't provide a usable
terminal boundary. Rules that every normalizer must follow:

1. Emit at most one `turn.completed` per actual turn
2. Prefer real harness terminal events over synthetic ones
3. If `reset()` was called mid-turn, drop any pending synthetic completion
4. State reset after completion must allow the next real `turn.started` through

Normalizers use a `_completed_for_turn` boolean (or equivalent) to guard against double-emit.

## Three Semantic Planes

These three modules answer different questions and must not be merged:

| Module | Role | Used by |
|---|---|---|
| `harness/semantics.py` | When does a drain end, what activity/signal states change | `SpawnManager`, drain loop |
| `normalization/synthetic.py` | Adapter for synthetic `meridian/*` event constants | Per-harness normalizers |
| `normalization/common.py` | Projection helpers for payload extraction | Per-harness normalizers |

Normalizers import `is_turn_boundary_event` from `synthetic.py` — **never** from `drain_policy`
directly. This is the only coupling point between the drain layer and normalization.

## Import Direction

The harness layer (`lib/harness/`) must not import from `lib/chat/`. Normalizers live in
`lib/chat/normalization/` so all `ChatEvent` imports stay inside the chat boundary:

```
lib/harness/     →  HarnessEvent (passed through ChatEventObserver seam)
lib/chat/        →  ChatEvent (normalizers own this projection)
```

## Per-Harness Specifics

**Claude (`claude.py`)** handles both streaming block events and aggregated message snapshots:
- `content_block_stop` for `tool_use` is non-terminal — the matching `tool_result` in a later
  aggregated `user` event owns the terminal `item.completed`
- Assistant fallback text on `result` is emitted only if no earlier streaming text was seen
- Block state is tracked in `_blocks: dict[int, _BlockState]` keyed by block index

**Codex (`codex.py`)** handles multiple live raw shapes:
- Turn and thread IDs may appear as snake_case, camelCase, or nested — extract defensively
- Tool `aggregatedOutput` stays on `item.completed` payload; it must not become `content.delta`

**OpenCode (`opencode.py`)** requires part-role tracking:
- `message.updated` is authoritative; refreshes `message.id → role` and `part.id → type` maps
- Only assistant-role `text`/`reasoning` parts become `content.delta`
- If only a terminal tool snapshot arrives without prior `item.started`, emit synthetic `item.started` first

## Uplinks

→ [KB: architecture/chat/normalization.md](/home/jimyao/.meridian/git/meridian-flow-docs/kb/architecture/chat/normalization.md)

## Lateral Links

→ [../.context/CONTEXT.md](../.context/CONTEXT.md) — normalization is layer 2; `ChatEventObserver` dispatches here
