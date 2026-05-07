# No-PTY Sidechannel Control Plane Design

Source of truth: [../requirements.md](../requirements.md)

## Recommendation

Meridian should split **terminal rendering ownership** from **session control ownership**.

- **Codex** and **OpenCode** should default to **native terminal inheritance** for interactive TUI rendering when Meridian also has a first-class side-channel observer/controller for that harness.
- **Claude** should remain **PTY-mediated** because Meridian does not have an equivalent first-class control/observation API for Claude.
- Meridian should continue to own:
  - ordered event observation;
  - hook dispatch;
  - permission/HITL routing;
  - injected user-message delivery;
  - durable session history and replay.

The core structural move is:

```text
real terminal owned by harness TUI
Meridian attached to side-channel control plane
```

## Package map

- [architecture/recommended-architecture.md](architecture/recommended-architecture.md) — target architecture, boundaries, data model, and dataflow
- [architecture/harness-launch-control-matrix.md](architecture/harness-launch-control-matrix.md) — per-harness terminal/control matrix and injection behavior
- [architecture/permission-hitl-routing.md](architecture/permission-hitl-routing.md) — runtime permissions and HITL routing design
- [architecture/migration-and-probes.md](architecture/migration-and-probes.md) — rollout order, fallback policy, risks, and required probes
- [architecture/decision-log.md](architecture/decision-log.md) — key decisions and rejected alternatives

## Outcome summary

### What changes

- Launch resolution becomes **per-harness terminal mode selection** instead of a global PTY choice.
- Managed primary sessions for Codex/OpenCode keep the existing observer/controller role, but the attached TUI process stops flowing through Meridian's PTY on supported platforms.
- Event persistence becomes the authority for hooks, replay, permission state, and interrupt fencing.
- Injection becomes a serialized control action with two explicit modes:
  - `interrupt_then_inject`
  - `inject_only`

### What does not change

- Claude remains PTY-wrapped.
- Meridian remains the durable authority for spawn/session state.
- Hook execution stays policy-side, not transport-side.
- Existing managed-primary sidecars (`primary_meta.json`, `history.jsonl`) remain the right persistence boundary, though their schema can evolve.

## Acceptance fit

This design package covers:

1. recommended architecture;
2. per-harness launch/control matrix;
3. event/control dataflow diagrams;
4. injection semantics by harness;
5. permission/HITL routing;
6. risks and probes still needed;
7. implementation boundaries and migration order.
