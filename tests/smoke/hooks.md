# Smoke: hooks

Hooks CLI — list, check, run.

## Setup (git required)

```bash
export SCRATCH=$(mktemp -d)
export MERIDIAN_HOME=$(mktemp -d)
export MERIDIAN_PROJECT_DIR=$SCRATCH
git -C "$SCRATCH" init --quiet
```

## hooks list — text output

```bash
cat > "$SCRATCH/meridian.toml" << 'EOF'
[[hooks]]
name = "test-hook"
event = "spawn.created"
command = "echo test"
EOF

uv run meridian hooks list --format text
```
- [ ] Exit 0
- [ ] stdout is non-empty (headers or rows)

## hooks list — JSON output

```bash
cat > "$SCRATCH/meridian.toml" << 'EOF'
[[hooks]]
name = "json-test-hook"
event = "spawn.finalized"
command = "echo json"
EOF

uv run meridian hooks list --format json
```
- [ ] Exit 0
- [ ] Valid JSON with `"hooks"` array key

## hooks check

```bash
uv run meridian hooks check --format text
```
- [ ] Exit 0
- [ ] Output shows requirement status

## hooks run — manual execution

```bash
cat > "$SCRATCH/meridian.toml" << 'EOF'
[[hooks]]
name = "manual-hook"
event = "spawn.finalized"
command = "echo manual-run"
EOF

uv run meridian hooks run manual-hook --format text
```
- [ ] No `Traceback` in stderr (may exit 0 or non-zero)

## hooks list — builtin registration structure

```bash
uv run meridian hooks list --format json
```
- [ ] Exit 0
- [ ] Each entry in `hooks` array has a `name` field
