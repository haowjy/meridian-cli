# History storage and retention

Meridian keeps readable JSONL transcripts and lifecycle metadata as files. A
rebuildable SQLite index accelerates discovery; it is not the only copy of history.
Copy a complete record bundle to preserve lifecycle facts as well as transcript
content. A bare JSONL file remains readable without the original harness.

## Index repair

```sh
meridian session index status
meridian session index rebuild
meridian session index rebuild --reset  # damaged dirty-source coordination
```

Rebuild uses authoritative loose records, session events, archive receipts and
available standard ZIPs in the configured destination. Direct out-of-band copies
require rebuild/import. Normal managed writes are discovered automatically.

To remove index data manually, stop all users of that runtime first and remove
only its `history-index/` directory. Do not remove `locks/` or unlink held locks.
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
meridian session search "phrase" --include-archives
meridian session restore HISTORY_UUID --archive /mnt/history/meridian/meridian-history-UUID.zip
```

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
