# Smoke: output formats

JSON and text rendering across commands.

## Setup

```bash
. tests/smoke/scripts/setup.sh
smoke_add_agent test
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
