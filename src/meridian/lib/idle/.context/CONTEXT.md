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
compaction ends the stretch's pings (a `precompute` compaction does not), as
does a ping that would go out after the cache is already cold.
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
session while the cache is warm. Core never compacts and does not see the choice.
