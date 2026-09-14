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
SQLite readers hold only the database gate. Rebuild holds catchup/root gates,
uses a fresh rollback-journal stage, drains all pending sources, then checkpoints
and closes the old WAL under the exclusive database gate before replacement.
Reset takes root exclusively, writes a new generation before dropping markers,
and rebuilds authority. Never unlink these lock identities.

External copies require explicit rebuild/import. Online repair uses `session
index rebuild`; damaged coordination requires `--reset`. Offline deletion of
`history-index/` is safe only after its runtime's users stop. Leave `locks/` alone.
Busy, disk-full, permissions and ordinary I/O errors are not corruption recovery.

## Identity and authority

History UUID identifies one transcript, not a reusable cN alias. Sessions retain
all generations; fork-start captures the source UUID before a chat can resume.
Headers carry portable origin/relationships. JSONL remains append-only across
retries, with attempt boundaries; lifecycle extractors ignore earlier attempts.
Missing native primary content is captured through harness transcript providers
after stop, never synthesized from a rendered report.

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

## Bounded preview projection

The same database holds disposable preview checkpoints separately from metadata.
`ops/session_preview.py` selects sources and feeds the shared harness normalizer
through a bounded accumulator; no independent transcript interpretation or FTS.
Metadata catch-up/automatic rebuild do not warm bodies. Explicit rebuild warms
through this path unless `--metadata-only`; metadata activity reads only the last
complete event (which can itself be large), not a transcript projection.

Selection reads eligible cache rows without catch-up or source access. Refresh
parses outside locks, then publishes through root/database/source synchronization
with build, generation, selected digest and source checks. Cached archived content
must have passed required-member byte verification for that selected snapshot.
Offline cache never substitutes a different snapshot; reading does not restore.

Managed append-only streams reuse complete-line checkpoints; changed native files
and OpenCode selected-session database snapshots reparse. Same-size edits, new
inodes and truncation invalidate checkpoints. Tail witnesses assume controlled
append-only growth, not arbitrary prefix edits plus append. External edits require
explicit rebuild. The browser exposes stale/updating, unavailable/offline and
clipping status, and refreshes an active selected row every two seconds.
