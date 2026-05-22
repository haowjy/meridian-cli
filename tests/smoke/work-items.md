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

## work set-worktree / clear-worktree

```bash
uv run meridian work start with-worktree-path
mkdir -p "$SCRATCH/worktree-target"
uv run meridian work set-worktree with-worktree-path "$SCRATCH/worktree-target"
uv run meridian work show with-worktree-path --json
```
- [ ] Exit 0
- [ ] JSON shows worktree path set to `$SCRATCH/worktree-target`

```bash
uv run meridian work clear-worktree with-worktree-path
uv run meridian work show with-worktree-path --json
```
- [ ] Exit 0
- [ ] JSON shows worktree path is null/empty

## manually assigned/shared worktree path is not removed by lifecycle

```bash
uv run meridian work start manual-a
uv run meridian work start manual-b
mkdir -p "$SCRATCH/shared-manual-worktree"
uv run meridian work set-worktree manual-a "$SCRATCH/shared-manual-worktree"
uv run meridian work set-worktree manual-b "$SCRATCH/shared-manual-worktree"
uv run meridian work done manual-a
test -d "$SCRATCH/shared-manual-worktree"
uv run meridian work delete manual-b
test -d "$SCRATCH/shared-manual-worktree"
```
- [ ] Both `test -d` checks pass
- [ ] Lifecycle output says the path is manually assigned / not removed
