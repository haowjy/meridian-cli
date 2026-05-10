# Smoke: output formats

JSON and text rendering across commands.

## Setup

```bash
export SCRATCH=$(mktemp -d)
export MERIDIAN_HOME=$(mktemp -d)
export MERIDIAN_PROJECT_DIR=$SCRATCH
mkdir -p "$SCRATCH/.mars/agents"
echo "# Test" > "$SCRATCH/.mars/agents/test.md"
```

## --json flag produces JSON for spawn dry-run

```bash
uv run meridian spawn -a test -p "probe" --dry-run --json
```
- [ ] Exit 0
- [ ] stdout is valid JSON object

## --json flag produces JSON for doctor

```bash
uv run meridian doctor --json
```
- [ ] Exit 0
- [ ] stdout is valid JSON object

## --json flag produces JSON for work current

```bash
uv run meridian work current --json
```
- [ ] Exit 0 (if no active work item, output may be `null`)
- [ ] stdout is valid JSON (null, string, or object)

## --format text produces human-readable output

```bash
uv run meridian --format text doctor
```
- [ ] Exit 0
- [ ] stdout does NOT start with `{` (not JSON)

## --format json explicit

```bash
uv run meridian --format json spawn -a test -p "test" --dry-run
```
- [ ] Exit 0
- [ ] stdout is valid JSON object
