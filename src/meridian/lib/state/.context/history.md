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

Cold builds have a separate initialization budget and latch an exhausted build;
contention is not a persistent failure. Reads cannot treat incomplete index coverage
as a negative result. Streaming retries temporarily unavailable descendant evidence;
selected loose rows still require authoritative lifecycle reads. Archived rows serve as
ancestry edges, not lifecycle authority.

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

KB `state/portable-history.md` covers the full design.
