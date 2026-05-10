# Smoke: work items

Work item lifecycle — list, start, done, delete, rename.

## Setup

```bash
export SCRATCH=$(mktemp -d)
export MERIDIAN_HOME=$(mktemp -d)
export MERIDIAN_PROJECT_DIR=$SCRATCH
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
