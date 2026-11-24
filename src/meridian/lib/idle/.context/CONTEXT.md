# Idle policy

A certain background wake holds the push until delivery or user return; a known
wake before cache expiry drops the warning. A subagent waiting for permission
does not qualify as a wake.

KB `spawns/idle-notifications.md` records the policy and the reasons behind it.

Core claims the warning once per stretch, whatever the harness, and sends it
through the configured notify routes. The Claude mod's keepwarm pings ride that
one warning (ping 1) and time the later pings itself
([README](../../../claude_runtime/meridian-idle/README.md)).
The keepwarm default and ping count (`[idle] keepwarm`, `keepwarm_pings`,
overridable under `[harness.claude.idle]`) reach the mod in the `idle config`
reply, and only an interactive primary acts on them. A main-conversation
compaction ends the stretch's prompts (a `precompute` compaction does not), as
does a prompt that would go out after the cache is already cold.
Do not make core re-claim `warn` per anchor to carry them: every ping would then
send another notice, and remembering the single notice needs a new `IdleState`
field, which older builds cannot read back (`extra="forbid"`).

The state reminder (`[idle] state_reminder`, default on, same `[harness.claude.idle]`
override) is decided here, not in the mod: `fire warn` answers `remind: true` when
the warning acts, the setting is on and the session has a work item (resolved the
way every "current item" is, so a `work switch` counts). The mod only submits the
prompt, so the `idle config` reply carries no setting for it. It rides the same
single warning as ping 1, for the same reason: re-claiming `warn` would send
another notice, and remembering the reminder needs an `IdleState` field older
builds cannot read. So the mod keeps the reminder owed in its own `$.state`
stretch record until it submits it, and a turn running at the warning only
delays it. The notice goes out before `remind` is decided. The mod, not core,
acts on the reminder's reply: a last line `compact-now` makes it compact the
session while the cache is warm, once the box is quiet; it measures its own
cold-cache deadline from the reminder's turn and stops if another compaction
lands first. Core never compacts and does not see the choice.

The sidecar hands `remind` to a sensor that takes prompts (`PromptSink` in
`harness/idle_types.py`); core names no harness. agy's sensor queues the reminder in the
conversation's inbox, so it waits for the session's next model call and offers no `compact-now`.

Keepwarm for such a sensor is timed here, not in a mod: `Stretch` (`keepwarm.py`) is an
in-memory record per stretch. Ping 1 goes out when `fire warn` acts (the reminder takes its
slot), each later one a `lead` after a non-return turn end of the stretch (the lead taken from
the arm that placed the warning, as Claude's mod does), up to `keepwarm_pings`, stopping a
minute before the cache would expire; a return cancels, and an `IdleEvent.prompt_replies` entry ending in
`stop-waiting` ends them. A sensor that could not deliver spends no slot. A sidecar with no record
(it restarted mid-stretch) sends none: core's `done["warn"]` persists, so it never re-warns, and
adding an `IdleState` field older builds cannot read is the alternative this rules out.
