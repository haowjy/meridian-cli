# Migration Order, Risks, and Probes

See also: [recommended-architecture.md](recommended-architecture.md), [permission-hitl-routing.md](permission-hitl-routing.md)

## Implementation boundaries

The design intentionally separates four changes that are easy to reason about independently.

### 1. Terminal-surface policy boundary

Owns:

- per-harness `terminal_surface_mode` resolution;
- compatibility override behavior;
- process-launcher selection for `native_inherit` versus `pty_mediated`.

Does not own:

- event normalization;
- permission routing;
- hook logic.

### 2. Managed control-plane boundary

Owns:

- side-channel backend lifecycle;
- ordered event acquisition;
- session/thread id discovery;
- attach command construction.

Does not own:

- terminal rendering;
- hook policy.

### 3. Control-action boundary

Owns:

- serialized interrupt and inject operations;
- response delivery for permission and user-input requests;
- turn fencing and stale-event classification.

Does not own:

- UI rendering;
- event replay formatting.

### 4. Consumer boundary

Owns:

- hook dispatch;
- permission presentation;
- UI replay;
- derived indexes and metadata projections.

Does not own:

- transport details.

## Migration order

### Phase 1 — introduce explicit terminal-surface mode

Goal:

- add resolved `terminal_surface_mode` without changing default behavior.

Exit gate:

- current Claude, Codex, and OpenCode launches behave exactly as before under compatibility defaults.

### Phase 2 — persist richer causal history

Goal:

- upgrade managed-primary history to include session, turn, item, request, and interrupt-fencing metadata.

Exit gate:

- history alone is sufficient to rebuild active turn, pending request, and stale-output state after restart.

### Phase 3 — add control action coordinator and broker-backed HITL

Goal:

- move interrupt, inject, and request-response flows behind one serialized control boundary.

Exit gate:

- Codex runtime requests no longer rely on auto-answer or reject-by-default behavior;
- pending requests survive process restart as pending state.

### Phase 4 — Codex native-terminal rollout

Goal:

- keep managed app-server control plane;
- stop wrapping `codex resume --remote` in Meridian PTY by default under `auto` mode.

Exit gate:

- resize and idle corruption regressions do not reproduce in smoke testing;
- interrupt fencing correctly contains stale old-turn output;
- permission prompts still round-trip correctly.

### Phase 5 — OpenCode native-terminal rollout

Goal:

- keep `opencode serve` control plane;
- use direct terminal attach and async injection path.

Exit gate:

- permission request event shape is verified end-to-end;
- `prompt_async` path works for inject-only;
- abort plus inject sequence is reliable.

### Phase 6 — compatibility cleanup

Goal:

- keep Claude on PTY;
- keep an escape hatch for Codex/OpenCode PTY compatibility mode;
- remove launch paths that silently violate control-plane guarantees.

Exit gate:

- no interactive mode silently drops hooks, injection, or permissions by falling back to black-box behavior.

## Risk register

### High risk

#### 1. Codex interrupted-turn stale output

Risk:

Late output from the interrupted turn could be mistaken for current-turn output, re-open hooks, or confuse replay.

Mitigation:

- persist causal ids;
- gate `interrupt_then_inject` on matching `turn/completed`;
- classify late old-turn output as stale, not current.

#### 2. Runtime permission regressions

Risk:

Switching away from PTY could accidentally restore a path where requests are rejected because no interactive broker is installed.

Mitigation:

- broker all runtime requests explicitly;
- treat unanswered as `pending`, never implicit reject;
- fail launch if required HITL cannot be honored.

#### 3. OpenCode permission-event uncertainty

Risk:

The response endpoint is documented, but the exact event surface for pending requests still needs verification.

Mitigation:

- hold default rollout behind a probe gate;
- use first-class documented control surfaces only.

### Medium risk

#### 4. Native attach preflight failure

Risk:

Backend starts but attach command or endpoint is unavailable.

Mitigation:

- allow compatibility fallback only before the session becomes user-visible;
- record which fallback happened and why.

#### 5. Replay-triggered duplicate hook actions

Risk:

A restart could re-fire injection or policy hooks from old history.

Mitigation:

- separate UI replay from side-effect consumer cursors;
- persist action ids and consumer cursors.

#### 6. Windows console quirks

Risk:

A native console attach on Windows could regress if Meridian accidentally routes the TUI through captured pipes or tries to emulate PTY behavior.

Mitigation:

- keep the TUI on inherited console handles;
- keep the backend headless;
- add Windows smoke coverage for native attach, cancel, and prompt injection.

## Required probes before implementation approval closes

### Codex

- verify runtime approval and user-input requests round-trip end-to-end in native-terminal mode;
- verify late old-turn output remains correctly fenced across interrupt plus inject;
- verify no-PTY attach behaves correctly on POSIX resize, idle, and focus return;
- verify Windows console inheritance with managed app-server plus native `codex resume --remote` attach.

### OpenCode

- verify exact SSE or event-bus shape for permission requests;
- verify permission reply endpoint closes the pending request correctly;
- verify `prompt_async` behavior during active turns and after abort;
- verify user-input prompts, if any, and their response path;
- verify Windows console inheritance with `opencode attach` against a managed server.

### Claude

- verify no behavior change at all for current PTY-backed interactive flows.

## Rollout policy recommendation

Use a staged default policy:

1. compatibility default retains existing behavior;
2. opt-in `auto` enables native-terminal sidechannel mode for Codex first;
3. after probe completion, flip Codex default to native-terminal `auto`;
4. repeat for OpenCode;
5. keep an explicit PTY compatibility override for both sidechannel harnesses until the smoke matrix is stable.

The rollout should be harness-by-harness, never global.
