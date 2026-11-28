# Code

- View: code

Meridian's Python packages, as layers: commands on top, files at the bottom. An arrow means
"uses". The import graph isn't a strict tree: launch, harness and streaming call into each other
around a running spawn. The arrows show the direction each layer is meant to depend in. The
rules that are enforced are listed under each box, and `src/meridian/dev/lints.py` checks them.
Paths are under `src/meridian/`.

```mermaid
flowchart TD
  cli["cli<br/><small>one module per command; parsing, output, JSON</small>"]:::package
  ops["ops<br/><small>what each command does</small>"]:::package
  launch["launch<br/><small>composes a launch, runs a primary</small>"]:::package
  harness["harness<br/><small>one adapter per harness, its store, its protocol</small>"]:::package
  streaming["streaming and idle<br/><small>drains events, completion hold, control socket</small>"]:::package
  reach["machines, message, inbox, hooks, notify<br/><small>reaching sessions, people and other machines</small>"]:::package
  config["config, catalog, context, bootstrap<br/><small>settings, Mars, agents, models, paths</small>"]:::package
  state["state<br/><small>every file Meridian owns, and its locks</small>"]:::package
  core["core<br/><small>domain types, refs, spawn lifecycle</small>"]:::package
  platform["platform, safety, telemetry<br/><small>atomic writes, processes, policy, logs</small>"]:::package
  surfaces["extensions, plugin_api, server<br/><small>MCP and plugin surfaces</small>"]:::package
  runtime["harness runtimes<br/><small>code that runs inside each harness</small>"]:::package
  tools["project tools<br/><small>artifacts, autosync, kg, mermaid, dev lints</small>"]:::package
  cli -->|"calls"| ops
  cli -->|"primary launch"| launch
  ops -->|"spawn create, continue"| launch
  ops -->|"locates and messages"| reach
  launch -->|"binds a spec for"| harness
  launch -->|"runs a spawn through"| streaming
  streaming -->|"drives the connection of"| harness
  launch -->|"bundle, profiles"| config
  ops -->|"rows, chats, work"| state
  launch -->|"publishes rows in"| state
  reach -->|"inbox, saved machines"| state
  state -->|"types from"| core
  state -->|"atomic writes, locks"| platform
  surfaces -->|"calls"| ops
  runtime -.->|"runs meridian commands"| cli
  tools -->|"uses"| state
  %% at surfaces 0 0
  %% at cli 330 0
  %% at runtime 660 0
  %% at ops 330 150
  %% at launch 165 300
  %% at reach 660 300
  %% at harness 0 450
  %% at streaming 330 450
  %% at config 660 450
  %% at state 330 600
  %% at tools 660 600
  %% at core 165 750
  %% at platform 495 750
```

## cli

`cli/`: the command surface, one module per command group, on Cyclopts. `main.py` sets up the
invocation: the project, `-C`, and the caller's identity, which `-C` into another project drops
from the environment. Output is text or `--json`, which scripts and other programs parse.

- Kind: package
- Paths: `src/meridian/cli`

## ops

`lib/ops/`: what each command does once it has its arguments: spawns (`ops/spawn/`), sessions,
work items, Mars, config, locating a reference (`locate.py`).

- Kind: package
- Paths: `src/meridian/lib/ops`

Under `ops/spawn/`, the depth guard and the sandbox check run before any row exists, and
foreground and background spawns meet at one function (`AGENTS.md` there).

## launch

`lib/launch/`: turns a request into a launch. Prepare it once, then bind it for each preview or
run. A harness adapter takes the bound argv, environment and permissions and composes nothing
itself. `launch/process/` runs a primary in the foreground, under a terminal it owns.

- Kind: package
- Paths: `src/meridian/lib/launch`

## harness

`lib/harness/`: everything harness-specific. Each harness has one adapter, its native store
reader, its projections (how Meridian's policy becomes the harness's flags and config) and
its connection (`connections/`). Everything outside reaches a harness through the contract in
`adapter.py` and the registry. A new harness is one adapter plus registration. Pi's own
completion engine (`pi_drain.py` and its `pi_*` parts) lives here too; streaming reaches it
through the connection's `drain_plan`.

- Kind: package
- Paths: `src/meridian/lib/harness`

Enforced: harness code doesn't import `lib/idle` (the launcher owns idle policy), and doesn't
start bootstrap at import time; code outside `lib/harness/` doesn't import a harness's own
module, `adapter_base` or `legacy_native_stores` (two legacy-import exceptions are listed).

## streaming

`lib/streaming/` drains a running harness's events, decides when a child is done (the
completion hold) and serves the control socket. `lib/idle/` decides the idle push and warning,
for every harness.

- Kind: package
- Paths: `src/meridian/lib/streaming`, `src/meridian/lib/idle`
- Machines: [the completion hold](../../src/meridian/lib/streaming/.context/state-machines.md), [an idle stretch](../../src/meridian/lib/idle/.context/state-machines.md)

## reach

How a session reaches anything outside itself. `lib/machines/` makes the SSH call to a saved
machine, `lib/message/` handles addresses and the people route, `lib/inbox/` is the machine
inbox, `lib/hooks/` runs user hook rows, and `lib/notify/` sends notifications.

- Kind: package
- Paths: `src/meridian/lib/machines`, `src/meridian/lib/message`, `src/meridian/lib/inbox`, `src/meridian/lib/hooks`, `src/meridian/lib/notify`
- See: [Saved machines](saved-machines.md), [Inbox and router](inbox.md)
- Machines: [an inbox message](../../src/meridian/lib/inbox/.context/state-machines.md)

## config

Settings and catalogs. `lib/config/` reads layered config (user, project, local) and edits
it with its comments kept. `lib/catalog/` holds agents, models and the Mars roots.
`lib/context/` resolves the KB and work directories. `lib/bootstrap/` prepares a runtime for a
read or a write.

- Kind: package
- Paths: `src/meridian/lib/config`, `src/meridian/lib/catalog`, `src/meridian/lib/context`, `src/meridian/lib/bootstrap`
- See: [Mars](mars.md)

## state

`lib/state/`: every file Meridian owns. Spawn rows, the chat journal, work items, machine IDs,
saved machines, idle state, the history index and the reaper. It is the authority. SQLite
indexes are projections that can be rebuilt.

- Kind: package
- Paths: `src/meridian/lib/state`
- See: [Data](data.md)
- Machines: [a spawn's status](../../src/meridian/lib/state/spawn/.context/state-machines.md), [chats, primaries and work items](../../src/meridian/lib/state/.context/state-machines.md)

Enforced: an authoritative write goes through `lib/state`'s wrappers or the atomic-write helpers.
The lints flag a raw write.

## core

`lib/core/`: what every layer shares. Domain types and the spawn status vocabulary, reference
parsing (`refs.py`), the spawn application service (`spawn_service.py`, which writes every
terminal status) and lifecycle events. `lib/utils/` has the shared JSON shapes.

- Kind: package
- Paths: `src/meridian/lib/core`, `src/meridian/lib/utils`

## platform

Below everything. `lib/platform/` holds atomic writes, file locks, process launch and process
scopes. `lib/safety/` holds budget, guardrail and permission policy. `lib/telemetry/` and
`lib/observability/` hold local JSONL telemetry and debug traces.

- Kind: package
- Paths: `src/meridian/lib/platform`, `src/meridian/lib/safety`, `src/meridian/lib/telemetry`, `src/meridian/lib/observability`

## surfaces

The ways other programs call Meridian besides the CLI. `server/` is an MCP server on stdio,
`lib/extensions/` is the extension registry and its command surface, and `plugin_api/` is the
stable surface for outside plugins, kept free of state internals.

- Kind: package
- Paths: `src/meridian/server`, `src/meridian/lib/extensions`, `src/meridian/plugin_api`

## runtime

Code that runs inside a harness, not inside Meridian: Claude Code plugins (`claude_runtime/`), Pi
extensions in TypeScript (`pi_runtime/`), the tool-rule evaluator shared by Pi and Codex
(`tool_policy_runtime/`), and Meridian's own Mars package (`mars/`: skills, agents, the agy hook
and MCP server). It reaches Meridian by running `meridian` commands.

- Kind: package
- Paths: `src/meridian/claude_runtime`, `src/meridian/pi_runtime`, `src/meridian/tool_policy_runtime`, `src/meridian/mars`

## tools

Features beside the main path. `lib/artifact/` publishes artifacts through Tailscale serve.
`lib/autosync/` is Python's half of context autosync, whose daemon is the Rust crate
`crates/meridian-sync`. `lib/kg/`, `lib/markdown/` and `lib/mermaid/` check a docs tree's links
and diagrams. `dev/` holds the source-tree lints and the version files.

- Kind: package
- Paths: `src/meridian/lib/artifact`, `src/meridian/lib/autosync`, `crates/meridian-sync`, `src/meridian/lib/kg`, `src/meridian/lib/markdown`, `src/meridian/lib/mermaid`, `src/meridian/dev`
