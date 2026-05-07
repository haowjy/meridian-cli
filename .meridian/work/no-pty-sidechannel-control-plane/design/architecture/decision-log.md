# Decision Log

See also: [recommended-architecture.md](recommended-architecture.md)

## D-1: Separate terminal ownership from control ownership

### Decision

For harnesses with first-class observer/controller APIs, Meridian should leave terminal rendering to the harness TUI and stay attached to the side-channel control plane.

### Why

The work item is about render corruption caused by extra terminal mediation, not about removing Meridian from session control.

### Rejected alternative

Keep Meridian in the terminal path and continue parsing or proxying render traffic.

Reason rejected:

- re-couples rendering and control;
- preserves the fragility that motivated the work;
- makes resize and repaint correctness Meridian's problem again.

## D-2: Keep Claude on PTY

### Decision

Claude remains PTY-mediated.

### Why

Claude does not currently offer the same class of side-channel observer/controller boundary Meridian already uses for Codex and OpenCode.

### Rejected alternative

Apply no-PTY globally.

Reason rejected:

- violates requirements;
- would remove current Claude observability rather than improve it.

## D-3: Introduce explicit `terminal_surface_mode`

### Decision

Terminal behavior should be an explicit resolved launch field, not an indirect consequence of process-launch capture behavior.

### Why

The architectural decision is policy, not mechanism. The harness capability and the launch policy should decide terminal ownership directly.

### Rejected alternative

Keep inferring PTY use from `output_log_path`, launcher defaults, or platform heuristics alone.

Reason rejected:

- hides a product-level behavior choice inside low-level launch machinery;
- makes harness-specific rollout brittle.

## D-4: Serialize outbound mutations through a control action coordinator

### Decision

Interrupts, injections, and runtime request replies should flow through one per-session coordinator.

### Why

Future hook-driven and external-event-driven injections will race unless Meridian has one ordering point.

### Rejected alternative

Let hooks, CLI commands, and UI handlers call transport methods directly.

Reason rejected:

- invites ordering bugs;
- makes recovery and replay semantics unclear;
- makes interrupt-then-inject especially fragile.

## D-5: Treat stale post-interrupt Codex output as stale data, not invisible data

### Decision

Persist late old-turn output with causal ids and classify it as stale after interrupt.

### Why

Codex interrupt can logically complete a turn while old subprocess output still streams. Hiding or dropping that data would make history inaccurate and debugging harder.

### Rejected alternative

Drop all old-turn output received after interrupt.

Reason rejected:

- loses forensic truth;
- risks hiding real backend behavior;
- makes replay nondeterministic.

## D-6: Pending permissions stay pending until explicitly resolved

### Decision

No runtime request may be auto-rejected merely because Meridian has not answered yet.

### Why

The user explicitly called out a prior default-deny regression.

### Rejected alternative

Map `confirm` or unhandled runtime requests to automatic reject.

Reason rejected:

- violates user intent;
- turns a wiring gap into a policy decision.

## D-7: Prefer OpenCode `prompt_async` over blocking `message` for inject-only

### Decision

Use `POST /session/:id/prompt_async` as the primary OpenCode inject-only transport.

### Why

It preserves a responsive control plane and matches the desired fire-and-forget injection semantics better than blocking `POST /session/:id/message`.

### Rejected alternative

Standardize on `POST /session/:id/message`.

Reason rejected:

- blocks until completion;
- couples injection latency to model runtime;
- is a worse fit for future external-event loops.

## D-8: Avoid transparent MITM or protocol proxying unless first-class APIs fail

### Decision

Do not design around websocket MITM, SSE proxying, or terminal-byte interception when the harness already exposes observer/control APIs.

### Why

The requirements explicitly prefer first-class side-channel APIs and the architectural goal is to reduce mediation, not move it sideways.

### Rejected alternative

Build a transparent proxy layer to regain total control.

Reason rejected:

- more code and more failure modes;
- harder cross-platform behavior;
- less reversible than using documented control surfaces.
