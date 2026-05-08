# Work Item Store

Source: `src/meridian/lib/state/work_store.py`, `src/meridian/lib/ops/work_lifecycle.py`

## Why Not JSONL

Work items deliberately use per-file mutable JSON rather than the JSONL event model used by spawns and sessions. The reason: a work item is tightly coupled to a scratch directory whose name matches the work item slug. When renamed, both the metadata file and the directory must move atomically. Append-only JSONL is good for immutable event streams; it's bad for mutable records whose key (the slug) changes and whose directory must move in sync.

## Storage Layout

```
.meridian/
  work-items/
    <work_id>.json       # WorkItem record: name, description, status, created_at
    work-items.rename.intent.json  # crash-safe rename intent (only present mid-rename)
  work/
    <work_id>/           # scratch directory for active work item
  archive/work/
    <work_id>/           # scratch directory for done/archived work item
```

`WorkItem` fields: `name` (slug), `description`, `status` (`open`|`done`|...), `created_at`.

## Slugification

`slugify(label)` normalizes a label to a URL-safe slug:
- Lowercase
- Whitespace and underscores → hyphens
- Non-alphanumeric/hyphen chars stripped
- Repeated hyphens collapsed
- Max 64 characters, trimmed of leading/trailing hyphens

## Atomic Writes

All work item metadata writes go through `atomic_write_text()` (tmp file + `os.replace()`). This means a crash mid-write leaves either the old file or the new file — never a partial JSON.

Locking uses `platform.locking.lock_file()` on the work-items directory sidecar (same cross-platform pattern as the JSONL stores).

## Rename Crash Safety

`work rename` is a multi-step operation that touches:
1. The metadata JSON file (`old_id.json` → `new_id.json`)
2. The payload `name` field inside the JSON
3. The scratch directory (`work/<old_id>/` → `work/<new_id>/`)
4. All child spawn `work_id` fields (update events in `spawns.jsonl`)
5. Active session `active_work_id` (update event in `sessions.jsonl`)

To make this crash-safe:
1. Write `work-items.rename.intent.json` with `{old_work_id, new_work_id, started_at}`
2. Execute steps 1–5 in order
3. Delete the intent file

`reconcile_work_store()` (called at startup and on read paths) detects leftover intent files and replays remaining steps:
- If old file exists and new file doesn't: rename the metadata file
- If new file exists but `name` field is stale: rewrite payload
- If old scratch dir exists and new doesn't: rename the directory
- Delete the intent file

This means even a crash mid-rename recovers correctly on next access.

## Archive Lifecycle

Work items transition through:
- `open` → scratch dir under `.meridian/work/<id>/`
- `done` → scratch dir moves to `.meridian/archive/work/<id>/`

`archive_work_item()` only moves the scratch dir and flips status to `done`. It does NOT update child spawn `work_id` fields or detach active sessions. `work_done_sync()` checks for active attachments (sessions and running spawns) and returns a warning if any exist, but takes no action to detach them.

Note: `work_rename_sync()` does update child spawn `work_id` fields and session attachments — but that's the rename path, not the done path.

`reopen` moves the scratch dir back to `work/`.

The `_locate_work_scratch_dir()` helper checks both locations and raises if a work item somehow has scratch dirs in both (should never happen; indicates a bug in archive lifecycle).

## Dashboard

`work_dashboard.py` groups active spawns by work item for display. Ungrouped spawns (no `work_id`) are surfaced separately. The dashboard is the default `meridian work` output — it provides a snapshot of all active work with attached spawns, statuses, and durations.
