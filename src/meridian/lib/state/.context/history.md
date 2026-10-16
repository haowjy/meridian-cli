# Portable history and derived discovery

Files and immutable ZIPs own history. SQLite and dirty markers contain no unique
domain facts. `history_index.py` alone projects metadata sources, acknowledges markers,
and selects the current history digest. Archive byte mechanics alone resolve
physical locations, remount hints and equivalent-copy verification. Lifecycle/control reads remain direct.

## Mutation and projection

Every indexed mutation holds root `locks/history-mutation.lock` shared, then its
source lock. `HistoryChanges.mark()` publishes a durable random token before the
source changes. Failure to mark prevents the mutation. Writers never need SQLite.

Catch-up captures a finite dirty set. Every authority is read under its source
lock, except that append-only `sessions.jsonl` may be projected to the committed
end observed by an open file descriptor while its writer holds the lock. In that
case its marker stays pending. A rewrite by tail repair is an atomic
replace, so a reader already holding the old inode sees the complete pre-rewrite
state; the cursor's inode/tail check forces a full replay on the next catch-up.
SQLite commits FULL, and marker acknowledgement always happens under the source
lock and removes only an unchanged token. Nonterminal spawn markers remain
pending; active stream appends may coalesce them. Terminal writes and late events
replace the token. Active activity is explicitly provisional.

Incremental session projection loads only the accepted-chat row and generation
rows for each chat touched after the cursor. A cursor reset still clears the
session projection and replays the full committed log. One read may observe only
a committed prefix of a multi-line `SessionBindings.commit()` batch. That is
acceptable for this disposable discovery projection; lifecycle and control
decisions continue to read session authority directly.

Lock order: catchup -> root mutation -> database -> source -> markers. Plain
SQLite readers hold only the database gate. Rebuild holds catchup/root gates, uses a fresh rollback-journal
stage, drains all pending sources, then checkpoints and closes the old WAL
under the exclusive database gate before replacement. Reset takes root
exclusively, writes a new generation before dropping markers, and rebuilds
authority. Never unlink these lock identities.

External copies require explicit rebuild/import. Online repair uses `session
index rebuild`; damaged coordination requires `--reset`. Offline deletion of
`history-index/` is safe only after its runtime's users stop. Leave `locks/` alone.
Busy, disk-full, permissions and ordinary I/O errors are not corruption recovery.

## Schema namespace and mixed-version overlap

`SCHEMA_VERSION` (in `history_changes.py`) names one projection namespace:
`history-index/history-v<N>.sqlite3`, its `.build-v<N>` stage, the
`history-index/pending-v<N>/` queue with its GENERATION,
`locks/history-{catchup,database,markers}-v<N>.lock` and the init latch.
A schema bump builds a fresh file from authority; there is no in-place migration,
because an older build that is still running (a background runner that outlives
an upgrade) must keep reading the file it understands. 0.6.7 and earlier use the
unversioned `history.sqlite3`, `pending/`, locks and latch. This build never opens
them and does not delete them.

Authority and its locks stay shared: `locks/history-mutation.lock` and source
locks. Each schema's writers mark only that schema's queue, so neither build
consumes, clears or resets the other's markers or GENERATION, and neither waits on
the other's catch-up or database gate. An older runner reads descendants through
its own index; that is what finalizes it (0.6.7's Pi drain treats any index error
as unknown evidence and does not complete while the error persists).

Writers of an older build still mutate shared authority during the overlap. Catch-up
re-reads every active loose spawn and the session-log cursor without a marker, so
runners that finish and primaries that stop after an upgrade still project. Spawns
an older build creates after this projection was built, or archive changes it
makes, need `session index rebuild`. The older index misses this build's writes.

Rollback: the older build's index is stale after any use of a newer build, and a
pre-release build of this branch migrated `history.sqlite3` to schema 6 in place.
After rolling back, run the older build's `meridian session index rebuild
--metadata-only`, or delete `history-index/history.sqlite3*` while no older process
uses the runtime.

## Initialization and read budgets

`history_index.py` classifies schema through read-only SQLite before entering the
existing catch-up gate. A missing index builds automatically, with an under-lock
recheck. Every caller gives that cold build its own 15-second initialization budget,
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
initialization; an all-warm pass never resets it. Status and cache counts inspect
only; peeks keep their five-millisecond cache path.

`descendant_projection()` uses the ordinary indexed-query path with one two-second
caller deadline: one bounded catch-up, then one recursive parent-index query.
Streaming treats contention or deadline exhaustion as temporarily unavailable
evidence and polls again instead of classifying it as a hard read failure. Traversal
retains archived rows as ancestry so a loose grandchild remains discoverable through
an archived intermediate, excludes the root, and terminates parent-edge cycles by
path. The result is discovery only. Callers must authoritatively read every selected
loose row before using lifecycle state; archived rows are traversal edges, not
projected lifecycle authority. A cold initialization or rebuild is still corpus-sized
even though the warm query and authoritative rereads are subtree-sized.

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
Those live-owner outcomes are retryable capture deferrals; archive passes report
them separately from corrupt, quarantined or missing-source errors. A deferral
during snapshot finalization aborts the caller-owned atomic stage.
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
The portable record digest (`retention_digest.py`) hashes a record's JSON as
stored, never a re-serialization through current models, so model growth cannot
invalidate older ZIPs. Checks of unchanged facts compare both sides through
current models.

The index keeps current metadata, independent locations, generation aliases and
session/work projections. Multiple ZIP copies remain candidates even with a
loose copy present. Only copies matching the selected portable digest are interchangeable; an
offline current snapshot never falls back to different older content. Published
snapshots remain separate until reclaim intent or explicit import selects them. Corrupt authority refuses complete coverage; it is never an empty result.

Snapshot reads use one resolver source kind, `snapshot`, selected in two cases only:
a restored historical record reads its local aggregate snapshot, and an archive-only
record this runtime did not reclaim itself (an import) streams the catalog-selected
ZIP member in place. Both bind the header to the history UUID and verify the seal.
A record this runtime reclaimed keeps reading its live binding, and a missing live
native source stays missing. Corpus search covers live native bindings only.

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
in replay. Restore is the sole historical-generation mint and uses the reserved
`historical-<uuid hex>` prefix. Append guards classify the event generation in O(1)
instead of folding the session log. The prefix rules (an import must carry it, a
live start must not) run only at append time. Replay must never enforce them: replay
cannot skip a row, so one older-build restore would fail every indexed read and
rebuild. Replay checks only that an import is inactive, plus the mutation rule. An
unprefixed restore already appended by an older build bypasses the append guard but
remains inoperable: operations refuse to continue or fork historical sessions. An unfinished older-build `RestorePlan`
does not resume; backward compatibility is not supported.

The target is that no per-spawn path replays `sessions.jsonl` while holding
`sessions.jsonl.flock`; the source lock must cover only bounded work. Known
violators are `SessionBindings.__init__`, the lookups in `start_session()`, model
selection and model observation deduplication, and `get_or_create_observed_chat()`.
[#591](https://github.com/haowjy/meridian-cli/issues/591) tracks their removal.
Exact session lookup returns raw authority, including nullable identity
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
