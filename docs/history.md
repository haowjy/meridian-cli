# History storage and retention

Meridian keeps readable JSONL transcripts and lifecycle metadata as files. A
rebuildable SQLite index accelerates discovery; it is not the only copy of history.
Copy a complete record bundle to preserve lifecycle facts as well as transcript
content. A bare JSONL file remains readable without the original harness.

## Index initialization and repair

The first indexed operation builds a missing or older-schema index automatically,
with a 15-second metadata budget and no progress bar. Normal warm queries keep
their two-second budget; workspace/global search shares one initialization budget
across its roots. These cooperative deadlines cannot interrupt a blocked filesystem
call. Automatic initialization does not warm every preview or move history into SQLite.

A genuine initialization failure is recorded outside the replaceable index directory.
Later automatic requests report the failure instead of repeatedly starting over.
Retry explicitly with `uv run meridian session index rebuild --metadata-only`;
success clears the failure. Manual metadata projection has a 60-second budget,
separate from archive import and native-search rebuilding. Cancellation and another
initializer holding a lock do not create persistent failures.

`session index status` inspects the schema, failure state and pending work without
initializing or catching up the index. A current schema does not prove complete
coverage. Native search status also reports fresh, stale, unindexed and unavailable
sources plus projection size. Newer unsupported schemas require an explicit decision to rebuild;
they are never silently queried or automatically downgraded.

```sh
meridian session index status
meridian session index rebuild  # rebuild metadata and native search; previews refresh lazily
meridian session index rebuild --metadata-only  # discovery metadata only
meridian session index rebuild --reset  # damaged dirty-source coordination
```

Rebuild uses authoritative loose records, session events, archive receipts and
available standard ZIPs in the configured destination. Direct out-of-band copies
require rebuild/import. Normal managed writes are discovered automatically.

To remove index data manually, stop all users of that runtime first and remove
only its `history-index/` directory. Do not remove `locks/` or unlink held locks.
Deleting SQLite alone does not clear a remembered initialization failure.
If a successful rebuild warns that its old failure marker could not be cleared,
restore write access and run a normal history read or rebuild before deleting the
index. Warm reads retry cleanup; read-only status does not.
Use the coordinated command for online rebuild. An offline archive location does
not erase locally retained archive metadata.

## Opt-in ZIP retention

Automation is off by default. Configure in `meridian.toml`:

```toml
[history.archive]
automatic = true
after_days = 30
destination = "/mnt/history/meridian"
interval_hours = 24
max_records = 256
max_uncompressed_bytes = 1073741824
```

Use a writable local or mounted destination that supports POSIX file locks,
hard-link publication and fsync. Unsupported publication fails without reclaiming
loose history.

Automation runs a finite maintenance pass after a primary stops, at the configured interval (daily by default).
It uses the same eligibility and verification mechanism as manual archiving.
Active records and dependencies are protected. Published ZIPs are never expired.

```sh
meridian session archive --list
meridian session archive --eligible --destination /mnt/history/meridian
meridian session archive --eligible --apply --destination /mnt/history/meridian
meridian session archive p123 --apply --destination /mnt/history/meridian
```

Without `--apply`, archive is a dry run. `--eligible` applies the age threshold to
last activity; explicit references select records without the age threshold but
cannot override activity/dependency protections. Each pass is bounded to 256
records or approximately 1 GiB (one oversized record may occupy its own ZIP).
These bounds are configurable. Repeat eligible passes to process a larger backlog.
Dry runs do not copy native harness transcripts: they report records requiring
preparation separately. Apply captures those records before final selection.

Every ZIP is independently verified against both source selection and member
bytes before originals can be removed. Changed sources retain their loose copy.
Interrupted removal leaves the verified ZIP readable; disposable retirement
residue is handled by existing startup staging cleanup. Failures may also leave
a private partial ZIP or restore stage; these are not published
archives and do not justify deleting source history.

## Read, transfer, and restore

```sh
meridian session import /mnt/history/meridian/meridian-history-UUID.zip
meridian session log HISTORY_UUID
meridian session search "phrase"
meridian session browse --include-archives
meridian session restore HISTORY_UUID --archive /mnt/history/meridian/meridian-history-UUID.zip
```

The browser always lists archived metadata and can preview a selected ZIP row.
Its `/` content search excludes archived rows unless started with
`--include-archives`. This adds their bound native transcripts, not ZIP content.
Corpus `session search` includes all bound chats by default and has no such flag.

Import explicitly selects a verified ZIP snapshot for direct reads without extracting it.
Rebuild and automatic recovery discover orphan ZIPs as snapshot-only metadata;
`archive --list` shows these alongside selected snapshots. Loose authority always
wins while present. Offline selected content can use an equivalent verified copy,
but never silently falls back to an older, different snapshot. Use it
again after moving a ZIP to update its locator. Restore accepts a path or known
archive UUID, selects only requested histories, preserves the ZIP, and assigns
new local aliases, printed alongside each history UUID. Browse labels these rows
as historical. Restored state is historical: foreign processes, leases and
harness continuation identifiers cannot become live. Repeating restore is safe;
conflicting changed content or session metadata is rejected rather than overwritten.
Unchanged restored records can be archived again without changing portable snapshot
identity. Synthetic local session metadata is not promoted into portable facts.

Content search excludes archives unless explicitly requested. Search budgets and
unavailable-content errors are reported as incomplete results, not “no matches.”
Matches already parsed from loose files survive budget exhaustion. A partial ZIP
member has not completed its checksum, so its matches are withheld; confirmed
matches from earlier complete records remain.
The existing `spawn archive` visibility flag is separate from ZIP retention.

## Browser previews

Selection shows a cached snippet immediately when available, labeled updating
until freshness is checked. Previews retain at most the latest ten normalized
messages in the current segment (16 KiB message text, 2 KiB setup); clipping is
labeled. Active selected sessions refresh every two seconds. This is not a full
conversation index; full logs and content search still read authoritative content.

A previously verified snippet can remain visible as `cached · archive offline`
when its selected ZIP is unavailable. Corrupt/unreadable content is labeled
unavailable, not current. Selecting a row or reading a ZIP never restores it.
Restore remains an explicit action.

## Pi journal views

Pi logs show recorded append order, including messages from earlier branches.
Compactions start segments with their recorded summaries. Branch summaries and
parent changes appear as annotations, not model turns or extra compactions.
These views do not reconstruct Pi's active prompt context. Unsupported material
is reported as incomplete rendering; retained raw bytes are not rewritten.
