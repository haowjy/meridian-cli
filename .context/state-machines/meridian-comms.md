# State machine: a routing in meridian-comms

One routing of one inbox item by the meridian-comms router. It is recorded as one line of
`~/.local/state/meridian-comms/routes.jsonl` on the bot host (`Outcome` in `src/routes.rs`, the
steps in `src/core/route.rs`). This page lives in the meridian-cli atlas because meridian-comms
has no state machine pages of its own yet. It belongs beside `src/routes.rs` in that repository.

```mermaid
stateDiagram-v2
  [*] --> accepted: meridian-comms route hands over the item
  accepted --> off: no [router] section or no OPENCODE_API_KEY
  accepted --> capped: over 20 routings a minute
  accepted --> listing: reads the item from the inbox
  listing --> deciding: candidates listed and filtered (project, cold)
  deciding --> offering: Jev picks a candidate
  deciding --> none: Jev answers none
  deciding --> second: Jev answers more
  second --> offering: the inbox-router agent says offer
  second --> none: it says none
  second --> asked: it says ask, posted in the thread
  offering --> offered: the notice arrived
  offering --> maybe_offered: the notice may or may not have arrived
  offering --> not_offered: Meridian sent nothing
  listing --> failed: the run broke
  deciding --> failed: the run broke
  offered --> [*]
  maybe_offered --> [*]
  not_offered --> [*]
  none --> [*]
  asked --> [*]
  off --> [*]
  capped --> [*]
  failed --> [*]
```

- **Final or not.** `off`, `capped` and `failed` leave the item waiting, and a later `route`
  for it runs again. Every other outcome is final (`Outcome::is_final`), and on restart the
  daemon rebuilds the set of routed items from the log (`Routing::resume`).
- **A reply to an `ask`** starts a second routing of the same item, logged with `again: true`.
- **Nothing here holds the text.** The text goes to Jev and to the agent, and to no post,
  command line or log line.
- Every outcome leaves the Meridian message waiting in the inbox until a session takes it. An
  offer grants nothing ([an inbox message](../../src/meridian/lib/inbox/.context/state-machines.md)).
