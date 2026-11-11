# Idle policy

A certain background wake holds the push until delivery or user return; a known
wake before cache expiry drops the warning. A subagent waiting for permission
does not qualify as a wake.

KB `spawns/idle-notifications.md` records the policy and the reasons behind it.

Core claims the warning once per stretch, whatever the harness, and sends it
through the configured notify routes. The Claude mod's keepwarm pings ride that
one warning (ping 1) and time pings 2 and 3 itself
([README](../../../claude_runtime/meridian-idle/README.md)).
Do not make core re-claim `warn` per anchor to carry them: every ping would then
send another notice, and remembering the single notice needs a new `IdleState`
field, which older builds cannot read back (`extra="forbid"`).
