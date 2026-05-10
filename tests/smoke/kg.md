# Smoke: kg (knowledge graph)

Document link topology — `kg graph` and `kg check`.

## Setup

```bash
export SCRATCH=$(mktemp -d)
export MERIDIAN_HOME=$(mktemp -d)
export MERIDIAN_PROJECT_DIR=$SCRATCH
```

## kg graph — clean directory

```bash
echo "# Hello\n\nNo links here." > "$SCRATCH/readme.md"
uv run meridian kg graph "$SCRATCH"
```
- [ ] Exit 0

## kg graph — linked docs

```bash
mkdir -p "$SCRATCH/docs"
echo "# Index\n\nSee [guide](guide.md)" > "$SCRATCH/docs/index.md"
echo "# Guide\n\nBack to [index](index.md)" > "$SCRATCH/docs/guide.md"
uv run meridian kg graph "$SCRATCH/docs"
```
- [ ] Exit 0
- [ ] `index` or `guide` appears in output (or exit 0 with no output)

## kg check — valid links

```bash
mkdir -p "$SCRATCH/docs"
echo "# A\n\nLink to [B](b.md)" > "$SCRATCH/docs/a.md"
echo "# B\n\nLink to [A](a.md)" > "$SCRATCH/docs/b.md"
uv run meridian kg check "$SCRATCH/docs"
```
- [ ] Exit 0

## kg check — broken links reported as warnings

```bash
mkdir -p "$SCRATCH/docs"
printf '# Orphan\n\nLink to [missing](does-not-exist.md)\n' > "$SCRATCH/docs/orphan.md"
uv run meridian kg check "$SCRATCH/docs"
```
- [ ] Exit 0
- [ ] stdout contains `warning: orphan.md:3 Broken link:`
- [ ] stderr contains `0 errors, 1 warnings`

## kg check --strict fails on warnings

```bash
uv run meridian kg check "$SCRATCH/docs" --strict
```
- [ ] Exit 1
- [ ] stdout contains `error: orphan.md:3 Broken link:`
- [ ] stderr contains `1 errors, 0 warnings`

## kg check — flag blocks and conflict markers

```bash
mkdir -p "$SCRATCH/docs"
cat > "$SCRATCH/docs/notes.md" << 'EOF'
# Notes
> [!FLAG]
<<<<<<< HEAD
left
=======
right
>>>>>>> branch
EOF

uv run meridian kg check "$SCRATCH/docs"
```
- [ ] Exit 1
- [ ] stdout contains `warning: notes.md:2 Flag block found`
- [ ] stdout contains `error: notes.md:3 Git conflict marker found`
- [ ] stdout contains `error: notes.md:5 Git conflict marker found`
- [ ] stdout contains `error: notes.md:7 Git conflict marker found`
- [ ] stderr contains `3 errors, 1 warnings`

## kg check — ignores findings inside fenced code blocks

```bash
mkdir -p "$SCRATCH/docs"
cat > "$SCRATCH/docs/example.md" << 'EOF'
# Example docs
Here is a code example:
````markdown
> [!FLAG]
```
<<<<<<< HEAD
=======
>>>>>>> branch
```
````
EOF

uv run meridian kg check "$SCRATCH/docs"
```
- [ ] Exit 0 (fenced content is not flagged)

## kg check — ignores inline code and prose mentions of flag syntax

```bash
mkdir -p "$SCRATCH/docs"
cat > "$SCRATCH/docs/examples.md" << 'EOF'
# Flag examples
Flags are searchable with `grep -r '\[!FLAG\]'`.
- `> [!FLAG]` markers are review callouts.
Add a `[!FLAG]` if something needs human review.
Plain [!FLAG] prose mention is documentation, not a flag.
EOF

uv run meridian kg check "$SCRATCH/docs"
```
- [ ] Exit 0

## kg graph — cwd default

```bash
echo "# Test" > "$SCRATCH/test.md"
cd "$SCRATCH" && uv run meridian kg graph
```
- [ ] Exit 0
