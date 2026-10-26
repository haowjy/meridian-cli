# Meridian spawn-watch plugin

A Claude Code mod that wakes a lead whose background children finished while
it sat idle. When a main-loop turn ends, the mod runs `meridian spawn collect`, which blocks until
child results are owed and ready; the mod submits
their status headers and `meridian spawn show <id>` lookup commands as a new turn,
then records the results as consumed with
`meridian spawn ack`. Meridian decides which children are owed, when they are
done, what the lead sees and what counts as consumed. The mod only senses idle,
runs the two commands, and keeps the delivery safe against the ways a turn can
fail to happen.

## What it does

| Claude hook | Action |
|---|---|
| `session.start` (TUI only) | Registers `/meridian-spawn-watch`. It starts no idle run: a resumed chat's prompt cache is cold, so a turn the user did not ask for would re-bill the whole context for a status ping. Results owed from before a crash, restart or reload wait until the user's first turn ends. Under `claude -p` (`isInteractive === false`, no surface) the mod stays inert for the whole session. |
| `turn.complete` without `agentId`, `reason: 'answer'` | After 2 s, starts `meridian spawn collect [--exclude <claimed ids>]`. |
| `turn.start` without `agentId` | Ends the pending 2 s delay, kills a running collector, ends a hold. If its text carries the nonce of a delivery, runs `meridian spawn ack <ids>`. |
| `prompt.edit`, `prompt.change` | Records when the user last touched the prompt box. |
| `session.end` | Ends the idle stretch. |

Subagent, teammate and compaction-fork turns raise `turn.start` and
`turn.complete` too, with `agentId` set. Both hooks ignore them; without the
filter, background agents would starve delivery.

The collector prints one JSON object and exits 0:

```
{"kind": "delivery", "ids": ["p12", "p13"], "text": "..."}
{"kind": "empty",    "ids": [],             "text": ""}
```

Anything else (non-zero exit, unparseable output, no usable ids) is logged and
retried at the next idle. Its stderr is diagnostics.

Delivery text is intentionally header-only: status/outcome, worker, failure and
distinct task directory when present, plus the exact `spawn show` lookup. It never
contains or reads the report body. A lead that wants the body runs the lookup.
A terminal report body shown by `spawn show` or `spawn report show` consumes the
notification, so the fallback announces only results the lead has not already seen.
`spawn status --full` follows the same rule. Summary-only output, report search, and
session logs do not consume it. `--no-observe` keeps any show or cancel result owed;
piped or redirected output otherwise still counts as shown.
Commands directed through `-C` never consume results in either project. In particular,
`spawn wait -C` is a pure read and `spawn ack -C` is refused.

A normal no-argument `meridian spawn wait` uses the same owed-child definition. It
leases its targets while running and consumes terminal results on return, so the
collector skips those children during the wait and does not deliver them afterward.
Wait takes at most the oldest 32 children that were already finished when target
resolution ran and renders them through the same header-only delivery formatter.
Running targets are uncapped and keep their full report output after wait blocks for
them. Already-finished rows do not affect waited-run counts, fail-fast, or exit status;
JSON returns them separately in `owed_spawns`. The remaining finished children stay
owed for the next wait or collection. Pi delivery receipts, cleared IDs, terminal
reports observed by CLI operations, and caller-issued cancels that report `cancelled`
count as consumed for both wait and collection, so those results are not replayed here.
`spawn wait --no-observe` deliberately does neither and may return the same finished
child again until a normal wait or `spawn ack` consumes it; its rendering is unchanged.

## Delivery, step by step

Delivery consumption is recorded only once the lead's conversation has the text.

1. The collector returns a `delivery`.
2. The mod **holds** while the prompt box has a draft, or was touched less than
   10 s ago (checked every second). A main-loop `turn.start` at any point up to
   the submit ends the hold.
3. The mod **claims** the ids in memory, so any collector started before the
   ack gets them as `--exclude`, and calls `$.prompt.submit` with the text
   under a `[meridian-spawn-watch delivery <nonce>]` line.
4. `submit` resolves when the turn starts, when the prompt is queued behind a
   running turn, or with `{ drop }`. On `drop` the claim is released and
   nothing is acked.
5. The **ack** runs on the main-loop `turn.start` whose text contains that
   nonce, and nothing else. The claim is released when the ack succeeds.

| Situation | Outcome |
|---|---|
| Esc (`aborted`), an API or context error, a refusal | No collector after that turn. Results stay owed; the next normal idle delivers them. |
| The user sends something before delivery | The `turn.start` kills the collector or ends the hold and releases the claim. Whatever the lead did not consume through wait, cancel, or a full report read is delivered at the next idle. |
| Prompt queued, then the queue is cleared | No turn carries the nonce, so nothing is acked. The next idle (2 s after a turn ends with no prompt behind it) releases the stale claim and the collector redelivers. |
| Ack fails (lock, disk, retired row) | Logged. The ids **stay excluded** until the mod reloads: releasing them would redeliver the same results at every idle. After a restart they are delivered once more. |
| Mod reload | The engine kills a running collector. The new copy gets `session.start` and starts with clean state; the next turn's end delivers what is owed. |
| Claude dies | `collect` exits when its parent changes. Nothing was consumed, so the resumed chat delivers after the user's first turn ends, never before it. |
| A peer, scheduled or Stop-hook turn runs before the user's first prompt | Its end begins a run like any main-loop turn's. The cache is warm by then, so the delivery costs no cold re-read. |
| `/clear` | The engine fires `session.end` and no `session.start`: the run ends, and nothing starts another until a turn ends. |

The 10 s rule has two sources because the engine splits them: typing and
deletion (a kill included) arrive as `prompt.edit`; history recall, clears and
a plugin's fill arrive as `prompt.change`. A draft that sits in the box holds
the delivery for as long as it is there.

`/clear` delivers into the cleared context. A child the lead cancels itself is
consumed only when the cancel reports `cancelled`; a `finalizing` cancel is delivered
later unless the lead sees its terminal report first.

## `/meridian-spawn-watch`

Prints the idle stretch's phase (`delay`, `collecting`, `holding`,
`submitting`), the last collector verdict, the claims in flight, and the last
log lines. The log lives in the plugin's `$.store` under `log` (last 100
lines). The mod writes nothing to the terminal, never reads Meridian config
files or `MERIDIAN_*` variables (the collector inherits them from the session),
and never writes under `~/.meridian`.

## Where it runs

`meridian claude` adds `--plugin-dir` for interactive launches when
`harness.claude.spawn_watch.enabled` is true (the default). That Python plugin
list is the only config gate: the mod itself asks nothing of Meridian at
`session.start`, unlike `meridian-idle`.

## Shared code with meridian-idle

The store log and a few small helpers are duplicated from `meridian-idle`. Claude plugins cannot share a module: the loader refuses an
import that leaves the plugin folder (`cannot import "../../shared/x.ts": it is
outside the plugin's folder`), and a symlink to a sibling directory is not
followed either. Each plugin must hold all of its own source.

## Developing it

```bash
claude plugin validate src/meridian/claude_runtime/meridian-spawn-watch
claude plugin test     src/meridian/claude_runtime/meridian-spawn-watch
```

The module is plain TypeScript the host runs directly: no build step, no
dependencies. `hooks/register.test.ts` plays `meridian spawn collect` and
`ack`, the prompt box and the engine's prompt queue, and covers each rule
above. Two things to know when changing it:

- The module validator wants every helper that takes `$` to be a top-level
  function declaration, not a closure inside `register`.
- The kit's `clock.advance` does not settle while a collector's stream is
  pending (it gives up after 1 s of real time), so the tests start the advance
  without awaiting it and wait for the collector instead.

Claude Code lays build-specific type files into the plugin directory it loads
with `--plugin-dir`: `.claude-plugin/types/**`, ignored by the repo and the
wheel, beside the committed 56-byte `tsconfig.json` that extends it. See the
`meridian-idle` README.
