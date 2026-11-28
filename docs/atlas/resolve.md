# Resolving a reference

- View: a request's path

What happens between a reference being typed and a command acting on it. The numbered path is a
claimed ID that belongs to another project, the case that needs no `-C`. The other arrows are a
reference that stays in the current project, and one that names another machine.

```mermaid
flowchart LR
  caller["caller<br/><small>a person or an agent types a reference</small>"]:::person
  parse["parse_ref<br/><small>classifies it by shape, no I/O</small>"]:::module
  forward["other_machine<br/><small>does it name another machine?</small>"]:::module
  locate["locate<br/><small>picks the runtime root, reads the row</small>"]:::module
  claims["ID claims<br/><small>one readlink</small>"]:::store
  theirs["the claim's runtime root<br/><small>another project's rows</small>"]:::store
  own["the current project's root<br/><small>from -C or the working directory</small>"]:::store
  command["the command<br/><small>show, wait, message, cancel, continue</small>"]:::module
  remote["machine run there<br/><small>the command runs on that machine</small>"]:::service
  caller -->|"1. c20417"| parse
  parse -->|"2. kind chat, no machine"| forward
  forward -->|"3. runs here"| locate
  locate -->|"4. asks for its root"| claims
  locate -->|"5. reads the row the claim points at"| theirs
  locate -->|"6. a located target: ID, runtime root, project root"| command
  locate -->|"no claim: an older ID, @latest, a native ID"| own
  locate -.->|"claimed elsewhere: is the same ID also here?"| own
  forward -->|"jimyao-minibox/c2041"| remote
  %% at caller 0 0
  %% at parse 330 0
  %% at forward 660 0
  %% at remote 990 0
  %% at locate 660 170
  %% at claims 990 170
  %% at command 330 340
  %% at own 660 340
  %% at theirs 990 340
```

## caller

A person at a terminal or an agent in a session. For an ID with a claim, neither needs to know
which project the session is in.

- Kind: person

## parse

`parse_ref` in `lib/core/refs.py`. Shapes: `p<digits>`, `c<digits>`, a selector (`@latest` and the
other names in `SELECTORS`), `<machine>/<spawn or chat ID>`, `inbox` and `<machine>/inbox`, and
anything else as a native session ID. An `@` address for a person is not a reference: only
`meridian message` takes one (`lib/message/address.py`).

- Kind: module
- Paths: `src/meridian/lib/core/refs.py`, `src/meridian/lib/message/address.py`

## forward

`machine_forward.other_machine`, the one place a reference turns into another machine. It refuses
an unknown machine, two machines in one command, `-C` applied here beside `<machine>/<id>`, and
a third machine named by a call that another machine started. `forward_if_remote` then sends a
command that may cross.

- Kind: module
- Paths: `src/meridian/cli/machine_forward.py`

## locate

`locate()` in `lib/ops/locate.py`, the one place the runtime root is chosen. For a spawn or chat
ID it asks the claim first. When the claim points at another project, it reads the row there
and takes that project's directory from the row. `locate_run` and `resolve_spawn_target` return
a `RunTarget` with the ID, runtime root and project root.

- Kind: module
- Paths: `src/meridian/lib/ops/locate.py`

## claims

`claim_root(id)` builds the link's path from the ID and reads it, with no scan, whatever the
number of projects. A claim directory hidden by a sandbox reads as no claim.

- Kind: store
- Paths: `src/meridian/lib/state/machine_ids.py`

## theirs

The runtime root the claim names. The row is read there under that root's locks, and a missing
row is "not found".

- Kind: store

## own

The runtime root of the project the command runs in. It serves IDs with no claim (issued before
0.28, or by an older build still running), selectors and native IDs. When a claim points
elsewhere and the same ID is also here, the command refuses (`RefAmbiguous`) and asks for `-C`
rather than guess.

- Kind: store

## command

What is done with the target. Reads and messages act in the target's root. `--continue`
launches in the target's own project, `--from` reads it and launches here. A command acts as its
caller only inside the caller's own runtime root. Toward another project it sets no parent and
collects no result, as `-C` does.

- Kind: module
- Paths: `src/meridian/lib/ops/spawn/context_ref.py`, `src/meridian/lib/ops/session_target.py`, `src/meridian/lib/ops/reference.py`

## remote

A reference with a machine name runs on that machine, which drops its own name from the reference
([A call to another machine](remote-call.md)). There is no current project there, so the ID
resolves by its claim, and an older ID needs `--machine <name> -C <path there>`.

- Kind: service
