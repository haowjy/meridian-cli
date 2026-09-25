# Portable history and derived discovery

Files and immutable ZIPs own history. SQLite and dirty markers contain no unique
domain facts. `history_index.py` alone projects metadata sources, acknowledges markers,
and selects the current history digest. Archive byte mechanics alone resolve
physical locations, remount hints and equivalent-copy verification. Lifecycle/control reads remain direct.

## Mutation and projection

Every indexed mutation holds root `locks/history-mutation.lock` shared, then its
source lock. `HistoryChanges.mark()` publishes a durable random token before the
source changes. Failure to mark prevents the mutation. Writers never need SQLite.

Catch-up captures a finite dirty set, reads each authority under its source lock,
commits SQLite FULL, then acknowledges only unchanged tokens. Nonterminal spawn
markers remain pending; active stream appends may coalesce them. Terminal writes
and late events replace the token. Active activity is explicitly provisional.

Lock order: catchup -> root mutation -> database -> source -> markers. Plain
SQLite readers hold only the database gate. Compatible outdated schemas migrate
in place under the exclusive database gate on the live WAL; they do not stage
or replace. Rebuild holds catchup/root gates, uses a fresh rollback-journal
stage, drains all pending sources, then checkpoints and closes the old WAL
under the exclusive database gate before replacement. Reset takes root
exclusively, writes a new generation before dropping markers, and rebuilds
authority. Never unlink these lock identities.

External copies require explicit rebuild/import. Online repair uses `session
index rebuild`; damaged coordination requires `--reset`. Offline deletion of
`history-index/` is safe only after its runtime's users stop. Leave `locks/` alone.
Busy, disk-full, permissions and ordinary I/O errors are not corruption recovery.

## Initialization and read budgets

`history_index.py` classifies schema through read-only SQLite before entering the
existing catch-up gate. Missing indexes rebuild. Compatible older schemas migrate
in place on the live WAL; untrusted older schemas, corrupt files, generation
mismatch and `--reset` rebuild. Both share the 15-second automatic metadata
phase and an under-lock recheck. A genuine owned-build failure is latched
in `history-index-init-failure.json`; manual publication clears it. Failed marker cleanup warns; warm catch-up retries it under the
same gate after verifying schema/generation. Status and cache-only reads never
perform this cleanup. Contention and cancellation are not persistent failures.

No-deadline reads initialize lazily, then begin the ordinary two-second budget.
Caller-owned deadlines never start another implicit build. Corpus search enumerates
roots without index access, shares one cold budget across roots, and starts its
ordinary deadline after actual initialization; an all-warm pass never resets it.
Status and cache counts inspect only; peeks keep their five-millisecond cache path.

`descendant_projection()` uses the ordinary indexed-query path: one bounded catch-up,
then one recursive parent-index query. Traversal retains archived rows as ancestry so a
loose grandchild remains discoverable through an archived intermediate, excludes the
root, and terminates parent-edge cycles by path. The result is discovery only. Callers
must authoritatively read every selected loose row before using lifecycle state; archived
rows are traversal edges, not projected lifecycle authority. A cold initialization or
rebuild is still corpus-sized even though the warm query and authoritative rereads are
subtree-sized.

## Identity and authority

History UUID identifies one transcript, not a reusable cN alias. Sessions retain
all generations; fork-start captures the source UUID before a chat can resume.
Headers carry portable origin/relationships. Attempt facts come from the live
fold, not replayed runner events. Native capture snapshots the exact bound key
for both primary and child spawns. No native source means no archive eligibility;
those records stay loose with a reason. Archive packs sealed records and does
not run capture. Post-stop maintenance receives the exact completed spawn ID,
not the latest chat projection.

Capture selection runs under the aggregate guard and requires agreement among
state, primary metadata and the linked generation. Known active owners and
unreleased live scopes block capture and are rechecked before publication.
Only a complete provider observation seals `native-transcript.jsonl`; a valid
seal is the idempotent no-op. Runner-stream existence is not capture evidence.

`native_snapshot.py` defines the separate sealed JSONL storage codec. It retains
raw native JSON text in source/ordinal envelopes; a final digest binds header,
body and observation metadata. Its writer serializes into a caller-owned atomic
stage and does not itself qualify native input or acquire aggregate locks. The
shared transcript reader recognizes this storage header even in renamed files,
validates incrementally, and exposes storage status independently of rendering.
The operations layer checks known native-session/harness or history-UUID bindings
at header consumption, before any body record; explicit file reads impose no
identity inferred from their filename. Reserved storage frames cannot fall through
to permissive native or append-stream interpretation after a damaged header.
Early close/budget exhaustion is partial; a valid empty seal is complete, not a
reason to select another source. Explicit snapshot reads validate the seal; normal chat reads resolve the native
key. Legacy ZIP stream members can be restored as inert bytes, never decoded.

The index keeps current metadata, independent locations, generation aliases and
session/work projections. Multiple ZIP copies remain candidates even with a
loose copy present. Only copies matching the selected portable digest are interchangeable; an
offline current snapshot never falls back to different older content. Published
snapshots remain separate until reclaim intent or explicit import selects them. Corrupt authority refuses complete coverage; it is never an empty result.

## Retention and restore

`retention_archive.py` owns inventory, ZIP bytes, independent verification and
append-only receipts. `ops/session_archive.py` owns eligibility and final
root-exclusive protection/witness revalidation. Publication never deletes
sources. Reclaim-prepared receipts precede removal; loose locations win while
both copies exist. Capture hashes under the shared root/source locks, bracketed by exact membership,
POSIX change-time and raw session/state witnesses shared with repeat restore.
The final exclusive gate rechecks this witness and fresh dependency protection,
not all file bytes. After verification, archive reclaim atomically retires the
aggregate into existing spawn staging and syncs both parents; recursive cleanup
runs only after root/spawn/scope locks are released. A failed parent sync leaves
the prepared receipt and staging intact, without returning a cleanup handle.
GC snapshots retirement entries then syncs both parents before disposal; recovery
syncs both parents before acknowledging an absent source. A persistent sync failure
therefore leaves the retirement pending, without another journal or lifecycle. Partial removal
there cannot hide the current ZIP. Startup staging GC may discard this residue;
it contains no unique authority, unlike restore plans/stages. Published ZIPs never expire. Reclaim orders dependents before dependencies, before
bundle limits, and refuses to retire a dependency while any loose record requires it.
The archive lock owns one destination staging name per runtime; retries clean only
that runtime's unpublished ZIP, never another runtime's staging or a published ZIP.

Restore extracts selected records into private stages outside ordinary spawn
stage GC, verifies copied bytes, assigns new local aliases, and publishes inert
historical state. A durable per-history restore plan spans publication/session
append for retry. Each plan owns its history-ID-named extraction stage; ordinary
failures clean the stage, and retry discards crash residue before fresh extraction.
No PID, lease, scope or harness continuation becomes live.
External ZIP extraction and hashing run outside the root gate. The final gate
revalidates aliases/history conflicts, publishes the staged aggregate and appends
the historical session. Historical guards belong in persistence/control seams,
not just the CLI.
Original source metadata remains provenance, not executable process ownership.

Restored session generations are immutable at their append boundary as well as
in replay. Exact session lookup returns raw authority, including nullable identity
fields; only exported capsules enrich local linkage, after raw-authority validation. Restore provenance binds the exact local state and historical session;
recapture validates those projections and all retained content before reusing
original portable session facts (including absence). Synthetic local sessions
never become new portable metadata. Existing-copy restore hashes under the shared
root gate and source lock, then rechecks POSIX inode/change-time and membership
witnesses plus exact session metadata under the short exclusive gate. Witnesses
only detect changes after checksum verification; they do not replace checksums.

## Search and previews

`native-search-v1.sqlite3` is a separate disposable projection. It stores native
keys, locators, freshness witnesses and normalized display entries, never chat
IDs. Search inverts accepted bindings from the session authority, validates
file/OpenCode witnesses, refreshes stale sources, then uses FTS5 trigrams only
to nominate candidates. Python's exact substring predicate decides matches.
Parser-version changes invalidate rows. Unsearched sources carry coverage reasons;
no index may select a chat's native source. Rebuild refreshes search unless
`--metadata-only`; status reports fresh/stale/unindexed counts and bytes.

Metadata activity uses spawn/session facts and sealed snapshot observations, not
runner-stream tails. `ops/session_preview.py` lazily refreshes bounded native
previews with the shared normalizer. Source signatures invalidate stale content;
there is no runner-stream cursor. Cache publication checks generation and the
selected source/digest under the existing locks. Incomplete or rendering-partial
snapshots do not count as compatible cached previews.
