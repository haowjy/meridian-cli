# Meridian system atlas

A clickable map of Meridian as released (0.28.3), the tools it works with, Mars (0.41.0) and
meridian-comms (`47bcfd3`), and how they meet. Each page has one diagram. Click a box for its
detail, or a stacked box for the page beneath it. The state machines sit beside the code that
owns them, listed in [the index](../../.context/state-machines/index.md).

```sh
node tools/atlas/serve.mjs docs/atlas/atlas.config.mjs   # http://localhost:4848/atlas/
node tools/atlas/check.mjs docs/atlas/atlas.config.mjs
```

## The pages

- [System](system.md)
- [Code](code.md)
- [Data](data.md)

Opened from the System page: [A spawn, start to end](spawn.md), [Mars](mars.md),
[meridian-comms](comms.md) (with [Inbox and router](inbox.md) and
[Agent to person](person-message.md)), and [Saved machines](saved-machines.md) (with
[Issuing an ID](ids.md), [Resolving a reference](resolve.md) and
[A call to another machine](remote-call.md)).

## What it covers

Meridian's own boxes name paths in this repository, and the check holds them there. Boxes for
Mars and meridian-comms name their code in prose ("Code (mars-agents): ..."), because the check
reads only this repository. Mars's internals are in its own atlas (`docs/atlas/` in mars-agents)
and its state machines beside its code. This atlas draws only where Meridian meets Mars.

A `Drift` line marks a box the next release changes. Meridian 0.29.0 (`release/v0.29.0`) moves
Mars's user level to `~/.mars`. A "What a rewrite could drop" paragraph on a state machine
names what exists only because Meridian rebuilds state after the fact from files, PIDs and
locks.

## Keeping it true

- A change that adds, removes or moves something a page names (a package, a path in a box, a
  process, a file under `~/.meridian/`) updates the page in the same commit. So does a change
  to a state machine's transitions, in its `state-machines.md`.
- Run the check before committing. It catches a path that's gone, a package directory no box
  covers, a state machine page missing from the index, and broken links. Whether each box's
  words and arrows are still true is for whoever changed the code to read.
- The import checks of the engine read JS and TS only, so arrows on the Code page are not
  checked against imports.
- `tools/atlas/` is a copy of the `/atlas` skill's engine. Change the engine there and copy it
  again.
