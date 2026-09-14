# Portable history and derived discovery

Files and immutable ZIPs own history. SQLite and dirty markers contain no unique
domain facts. `history_index.py` alone projects sources, acknowledges markers,
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
root-exclusive protection/fingerprint revalidation. Publication never deletes
sources. Reclaim-prepared receipts precede removal; loose locations win while
both copies exist. After verification, archive reclaim atomically retires the
aggregate into existing spawn staging before recursive cleanup. Partial removal
there cannot hide the current ZIP. Startup staging GC may discard this residue;
it contains no unique authority, unlike restore plans/stages. Published ZIPs never expire.

Restore extracts selected records into private stages outside ordinary spawn
stage GC, verifies copied bytes, assigns new local aliases, and publishes inert
historical state. A durable per-history restore plan spans publication/session
append for retry. No PID, lease, scope or harness continuation becomes live.
External ZIP extraction and hashing run outside the root gate. The final gate
revalidates aliases/history conflicts, publishes the staged aggregate and appends
the historical session. Historical guards belong in persistence/control seams,
not just the CLI.
Original source metadata remains provenance, not executable process ownership.
