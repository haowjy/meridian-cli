# Spawn operations

Create, continue and fork enforce the caller boundary before write bootstrap.
Continue and fork recheck passthrough arguments found in their source snapshot:
native flags can override the resolved sandbox or approval policy. The harness
escape runs outside the native sandbox, so refusal precedes state writes.

An observing wait pins a launch, not just a spawn ID, so a continue does not
replace the result being waited on. No-argument wait includes the caller's
transitive active descendants and owed direct background results, while excluding
siblings and ancestors. Collection records exactly which launches were delivered;
acknowledgement consumes those launches even if the spawn has since continued.

Consumption is scoped to the caller: reading a result from a nested non-owner
cannot consume the primary's child. Cross-project CLI reads do not observe results.
A failed observation must not replace a result already shown.

Cancel and destructive operations select stored status, not the derived display
status: a dead launch still needs its outcome persisted before cleanup.

Native-ID continuation preserves the supplied native ID and its harness namespace;
the chat's newest spawn can refer to a different native session. Exact continuation
rejects launch identity and policy changes before replay.
