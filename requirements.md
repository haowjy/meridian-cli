# Managed backend lifecycle

## Problem

A managed harness backend (opencode `serve`, codex app-server) has a lifecycle:
it is launched (often detached), a session must be observed, it runs and must
stay alive, it dies (cleanly, by stalling, or by hard crash of its worker), and
it must then be reaped with its spawn finalized.

**No single component owns this lifecycle.** Each stage, and each death mode, is
handled in whichever file was nearest when the bug was found. A structural audit
counts ~13 files holding a piece of "is this backend alive / should we tear it
down." That fragmentation is the root cause; the individual bugs below are all
the same gap seen from different angles.

## Observed failures (all one bug class)

1. **Orphan survival.** A detached backend outlives a hard-crashed worker;
   nothing reaps it; it holds a shared resource (`opencode.db`) and wedges the
   next launch.
2. **Takeover-phase wedge.** A spawn stuck at `worker_takeover_started` — backend
   up, session never observed, `harness_session_id` null — stays `running`
   forever. **No phase before `running` has a bounded timeout.** (Live specimens:
   p1532, p3830.)
3. **Liveness false-positive.** An agent doing legitimate long-but-quiet work
   (deep reasoning, blocking on a child spawn) is killed by a raw silence timer
   that cannot distinguish "busy" from "dead." Made fatal by the supervision fix.
4. **Never-finalized.** A spawn whose worker is dead or wedged is never
   transitioned to a terminal state; the reaper misses child-spawn backends.
5. **Phantom session resolution.** A child with no observed session resolves
   `session log` up its ancestry to a session id whose file does not exist.

## Required end state (solution-free)

- **Convergence.** Every death path — clean exit, in-process stall, hard worker
  crash — reaches the same observable end state: backend process reaped, spawn
  finalized. No path may leave a spawn `running`.
- **Every phase is bounded.** Launch → observe-session → run each has a deadline;
  a wedge in any phase is detected and finalized, not left running indefinitely.
- **Liveness reflects progress, not chatter.** Legitimate long-but-quiet work is
  not killed; a genuinely dead or wedged backend is. Silence alone is not death.
- **Single identity owner.** Backend pid and session id are recorded once, by one
  owner; log/session resolution never yields a phantom.
- **Recovery is startup.** A crash mid-lifecycle is reconciled on the next status
  pass from persisted facts (crash-only; atomic writes).
- **Windows is first-class.** Parent-death linkage and teardown are cross-platform.

## Constraints

- No backwards compatibility; the schema may change freely to get this right.
- The verified PR #320 pieces (parent-death linkage, reaper crash-recovery,
  orphan repro scripts) land as the **first consumers** of the new owner, not as
  scattered code to be refactored later.
