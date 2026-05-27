# Smoke: work items

Work item lifecycle — list, start, done, delete, rename.

## Setup

```bash
. tests/smoke/scripts/setup.sh
```

## work list — empty

```bash
uv run meridian work list --json
```
- [ ] Exit 0
- [ ] Valid JSON object
- [ ] `work_items` or `items` array is empty (or absent)

## work start creates an item

```bash
uv run meridian work start smoke-test-item
```
- [ ] Exit 0

```bash
uv run meridian work list --json
```
- [ ] Exit 0

## work list shows created item

```bash
uv run meridian work start visible-item
uv run meridian work list --json
```
- [ ] Exit 0
- [ ] `visible-item` appears in JSON output

## work done archives an item

```bash
uv run meridian work start to-archive
uv run meridian work done to-archive
```
- [ ] Exit 0

## work delete removes an empty item

```bash
uv run meridian work start to-delete
uv run meridian work delete to-delete
```
- [ ] Exit 0

## work delete --force removes item with artifacts

```bash
uv run meridian work start artifact-item
mkdir -p "$SCRATCH/.meridian/work/artifact-item"
echo "# Notes" > "$SCRATCH/.meridian/work/artifact-item/notes.md"

# At least one of these must succeed
uv run meridian work delete artifact-item
# or:
uv run meridian work delete artifact-item --force
```
- [ ] One of the two delete commands exits 0

## work rename

```bash
uv run meridian work start original-name
uv run meridian work rename original-name new-name
```
- [ ] No `Traceback` in stderr (may exit 0 or non-zero depending on feature state)

## work start --task-dir sets metadata (non-session safe)

```bash
mkdir -p "$SCRATCH/task-dir-target"
uv run meridian work start with-task-dir --task-dir "$SCRATCH/task-dir-target"
uv run meridian work show with-task-dir --json
```
- [ ] Exit 0
- [ ] JSON shows `task_dir` set to `$SCRATCH/task-dir-target`

## work task-dir set / clear requires session-attached active work

```bash
mkdir -p "$SCRATCH/task-dir-target-2"
uv run meridian work task-dir "$SCRATCH/task-dir-target-2"
```
- [ ] Exits non-zero
- [ ] Error mentions no active work item

```bash
uv run meridian work task-dir --clear
```
- [ ] Exits non-zero
- [ ] Error mentions no active work item

## work task-dir print behavior

```bash
uv run meridian work clear
uv run meridian work task-dir
```
- [ ] Exit 0
- [ ] Prints project root when no active work item is attached

- [ ] (Set/clear form needs a session-attached active work item; use `work start --task-dir` in non-session shells.)

## task_dir lifecycle is metadata-only (filesystem untouched)

```bash
mkdir -p "$SCRATCH/shared-task-dir"
uv run meridian work start manual-a --task-dir "$SCRATCH/shared-task-dir"
uv run meridian work start manual-b --task-dir "$SCRATCH/shared-task-dir"
uv run meridian work done manual-a
test -d "$SCRATCH/shared-task-dir"
uv run meridian work delete manual-b
test -d "$SCRATCH/shared-task-dir"
```
- [ ] Both `test -d` checks pass
- [ ] Lifecycle output does not attempt to remove or mutate the shared directory
