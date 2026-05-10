# Smoke: spawn error paths

Clean failures without tracebacks on invalid input.

## Setup

```bash
export SCRATCH=$(mktemp -d)
export MERIDIAN_HOME=$(mktemp -d)
export MERIDIAN_PROJECT_DIR=$SCRATCH
mkdir -p "$SCRATCH/.mars/agents"
echo "# Reviewer" > "$SCRATCH/.mars/agents/reviewer.md"
echo '[settings]' > "$SCRATCH/mars.toml"
echo 'models_cache_ttl_hours = 24' >> "$SCRATCH/mars.toml"
```

## Unknown model rejected

```bash
uv run meridian spawn -a reviewer -p "test" -m "definitely-not-a-model-xyz" --dry-run --json
```
- [ ] Exit non-zero
- [ ] No `Traceback` in stdout
- [ ] No `Traceback` in stderr

## Invalid spawn ID

```bash
uv run meridian spawn show no-such-spawn-xyz --json
```
- [ ] Exit non-zero
- [ ] No `Traceback` in stdout
- [ ] No `Traceback` in stderr

## Batch error paths — all traceback-free

```bash
uv run meridian nonexistent-cmd
```
- [ ] No `Traceback` in stdout or stderr

```bash
uv run meridian config get does.not.exist.key
```
- [ ] No `Traceback` in stdout or stderr

```bash
uv run meridian spawn show no-such-spawn
```
- [ ] No `Traceback` in stdout or stderr

## Error output goes to stderr

```bash
uv run meridian spawn show invalid-spawn-id-xyz --json
```
- [ ] Exit non-zero
- [ ] Error information appears in stderr (not stdout)
