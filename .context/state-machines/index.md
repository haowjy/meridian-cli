# State machines

Every state machine in Meridian, beside the code that owns its transitions, and the two in the
tools Meridian works with. Each page draws the machine as the code has it, its rules and their
reasons, and what a rewrite could drop.

## Meridian

| Machine | What it is the state of | Page |
|---|---|---|
| A spawn's status | one agent run: `running`, then one of four terminal states | [state/spawn](../../src/meridian/lib/state/spawn/.context/state-machines.md) |
| Process-scope cleanup | what a finished spawn still has to release | [state/spawn](../../src/meridian/lib/state/spawn/.context/state-machines.md#process-scope-cleanup-claim) |
| A chat's generation | one chat in `sessions.jsonl`: live, stopped | [state](../../src/meridian/lib/state/.context/state-machines.md) |
| A primary's activity | what a running primary is doing | [state](../../src/meridian/lib/state/.context/state-machines.md#a-primarys-activity) |
| A work item | active or done, by where its directory is | [state](../../src/meridian/lib/state/.context/state-machines.md#a-work-item) |
| The completion hold | when a resident child is finished | [streaming](../../src/meridian/lib/streaming/.context/state-machines.md) |
| An idle stretch | the push and warning for a quiet session | [idle](../../src/meridian/lib/idle/.context/state-machines.md) |
| An inbox message | waiting, taken, pruned | [inbox](../../src/meridian/lib/inbox/.context/state-machines.md) |

A launch's startup phases (`launching_subprocess` to `harness.ready`) are telemetry, not stored
state, and have no page.

## Around Meridian

| Machine | Where | Page |
|---|---|---|
| A routing | meridian-comms router, `routes.jsonl` | [meridian-comms.md](meridian-comms.md) |
| A `mars sync` and its recovery | mars-agents | `src/sync/.context/state-machines.md` in mars-agents, indexed by its `.context/state-machines/index.md` |
