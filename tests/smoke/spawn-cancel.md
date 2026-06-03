# Spawn Cancel

Manual smoke checklist for spawn-level cancellation semantics.

## Setup

Use an isolated scratch project and cheap model aliases where possible.

```bash
export MERIDIAN_HOME="$(mktemp -d)"
scratch="$(mktemp -d)"
cd "$scratch"
meridian init
```

## Background CLI runner cancellation

1. Start a long-running background spawn:
   ```bash
   meridian spawn --bg -a meridian-subagent -m gpt-5.4-mini \
     -p 'Run a shell sleep for 120 seconds, then report DONE.'
   ```
2. Capture the printed spawn id, then cancel it:
   ```bash
   meridian spawn cancel pNNN
   meridian spawn show pNNN --no-report
   ```

Expect:
- `state.json` contains `cancel_intent`.
- Final status converges to `cancelled` with exit code `130` (unless it had already completed).
- No retry attempt starts after the cancel request.
- No long-running sleep child remains.

## Codex/OpenCode-style backend wrapper

Run the same background scenario with a Codex or OpenCode profile/model available in the
project, then cancel from another shell.

Expect runner-first cancellation: the Meridian runner exits, backend scopes are cleaned as
fallback containment, and repeated `backend` scopes remain independently releasable.

## Pi long-running cancellation

```bash
meridian spawn --bg --harness pi -m openai-codex/gpt-5.4-mini \
  -p 'Run a shell sleep for 120 seconds, then report DONE.'
meridian spawn cancel pNNN
meridian spawn show pNNN --no-report
```

Expect final status `cancelled`, not `failed` with `error=cancelled`.

## Reaper recovery

If a runner is killed after `cancel_intent` is written but before finalization, run any read path:

```bash
meridian spawn status pNNN
```

Expect stale active/finalizing cancelled spawns to reconcile to `cancelled`, while a spawn that
already wrote a durable completion report remains `succeeded`.
