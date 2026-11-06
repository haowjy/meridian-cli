# Portable history and derived discovery

Files and verified ZIPs own history; SQLite is disposable. Lifecycle and control
decisions read authority directly. Indexed writers mark a source before mutation;
failed marking prevents the mutation. Catch-up acknowledges only an unchanged
marker under the source lock, so a concurrent write cannot be lost.

Index lock order is catchup → root mutation → database → source → markers.
The index and coordination namespace changes with schema and session format:
an older runner may still need its own index after an upgrade. Do not migrate or
reset another version's projection in place.

## Initialization and read budgets

`history_index.py` classifies schema through read-only SQLite before entering the
existing catch-up gate. A missing index builds automatically, with an under-lock
recheck. Every caller gives that cold build its own 60-second initialization budget,
separate from any caller deadline, and exhaustion is sticky: the failure is latched in
`history-index-init-failure-v<schema>.json`; manual publication clears it. Failed marker cleanup warns; warm catch-up retries it under the
same gate after verifying schema/generation. Status and cache-only reads never
perform this cleanup. Corrupt files, a foreign schema in this schema's file,
generation mismatch and `--reset` need an explicit rebuild. Contention is not a
persistent failure.

After lazy initialization, ordinary reads make one catch-up attempt bounded by
`QUERY_TIMEOUT` (two seconds).
A caller-owned deadline replaces that default. Source-lock contention, an exhausted
attempt, and post-preflight replacement are `HistoryIndexBusy`; pending source names
stay on `IndexCoverage`. Readers never answer from partial coverage and do not retry
inside the index. Corrupt/incompatible metadata, generation errors, and real I/O or
SQLite failures are not classified as contention.

The ordinary read deadline does not cancel the separately owned cold build, but the
same absolute caller deadline still bounds the catch-up attempted afterward. Corpus
search still enumerates roots without index access, initializes explicitly, shares
one cold budget across roots, and starts its ordinary deadline after actual
initialization; an all-warm pass never resets it. Descendant evidence likewise
initializes before starting its two-second caller deadline. Status and cache counts
inspect only; peeks keep their five-millisecond cache path.

`descendant_projection()` uses the ordinary indexed-query path with one two-second
caller deadline: one bounded catch-up, then one recursive parent-index query.
Streaming treats contention or deadline exhaustion as temporarily unavailable
evidence and polls again instead of classifying it as a hard read failure. Traversal
retains archived rows as ancestry so a loose grandchild remains discoverable through
an archived intermediate, excludes every root (the anchor takes one or many), and terminates parent-edge cycles by
path. The result is discovery only. Callers must authoritatively read every selected
loose row before using lifecycle state; archived rows are traversal edges, not
projected lifecycle authority. A cold initialization or rebuild is still corpus-sized
even though the warm query and authoritative rereads are subtree-sized.

## Retention and restore

Hash stored record JSON as stored, not after parsing through current models: new
model defaults would invalidate an older archive's digest. Checks comparing
unchanged facts pass both sides through current models.

Capture hashes under shared ownership, then revalidates membership and change
time under the exclusive gate. Witnesses detect changes after hashing; they do
not replace checksums. Publication does not delete sources. Reclaim receipts
precede removal, and cleanup follows release of root, spawn and scope locks.

Restore publishes inert historical records. Its generation prefix is checked
at append, not replay: an older accepted restore must remain readable. A live
binding whose native transcript is missing does not fall back to a retained
snapshot. Imported or restored records can read their own sealed snapshot.

KB `architecture/state-system/portable-history.md` covers the full design.
