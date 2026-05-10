# Smoke: context

Context path resolution — work, kb, strategy, verbose, JSON.

## Setup

```bash
export SCRATCH=$(mktemp -d)
export MERIDIAN_HOME=$(mktemp -d)
export MERIDIAN_PROJECT_DIR=$SCRATCH
```

## context work — no active item

```bash
uv run meridian context work
```
- [ ] Exit 0
- [ ] stdout is empty (no active work item attached)

## context kb

```bash
uv run meridian context kb
```
- [ ] Exit 0
- [ ] stdout: `$SCRATCH/.meridian/kb`

## context work.archive

```bash
uv run meridian context work.archive
```
- [ ] Exit 0
- [ ] stdout: `$SCRATCH/.meridian/archive/work`

## context strategy (configured source)

```bash
cat > "$SCRATCH/meridian.toml" << 'EOF'
[context.strategy]
source = "git"
remote = "git@github.com:meridian-flow/docs.git"
path = "voluma-bio/strategy"
EOF

uv run meridian context strategy
```
- [ ] Exit 0
- [ ] stdout ends with `/voluma-bio/strategy`

## context --verbose

```bash
uv run meridian context --verbose
```
- [ ] Exit 0
- [ ] `strategy:` appears in output
- [ ] `path: voluma-bio/strategy` appears in output
- [ ] `resolved: $SCRATCH/.meridian/kb` appears in output
- [ ] `archive_resolved: $SCRATCH/.meridian/archive/work` appears in output

## context --json

```bash
uv run meridian context --json
```
- [ ] Exit 0
- [ ] Valid JSON
- [ ] `active_work_dir` is `null`
- [ ] `kb_source == "local"`
- [ ] `kb_resolved` equals `$SCRATCH/.meridian/kb`
- [ ] `work_resolved` equals `$SCRATCH/.meridian/work`
- [ ] `work_archive_resolved` equals `$SCRATCH/.meridian/archive/work`
