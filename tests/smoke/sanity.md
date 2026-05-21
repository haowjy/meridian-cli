# Smoke: Sanity

Critical command surface — help, version, doctor, spawn list.

## Setup

```bash
. tests/smoke/scripts/setup.sh
smoke_add_agent test
```

## Help and version

```bash
uv run meridian --help
```
- [ ] Exit 0
- [ ] `meridian` or `spawn` appears in output
- [ ] `spawn`, `models`, and `work` all appear in output

```bash
uv run meridian --version
```
- [ ] Exit 0
- [ ] Output contains a version string matching `\d+\.\d+` (e.g. `0.1.2`)

## Config show

```bash
uv run meridian config show
```
- [ ] Exit 0
- [ ] Output contains `agent` (e.g. `primary.agent`)

## Models list redirect

```bash
uv run meridian models list
```
- [ ] Exit 1
- [ ] stderr contains `meridian mars models list`
- [ ] stdout is empty

## Doctor

```bash
uv run meridian doctor --prune
```
- [ ] Exit 0

```bash
uv run meridian doctor --help
```
- [ ] Exit 0
- [ ] `--prune` present in output
- [ ] `--global` present in output
- [ ] `--kill-orphans` present in output

## Spawn list

```bash
uv run meridian spawn list --json
```
- [ ] Exit 0
- [ ] JSON output contains `"spawns"` key with an array value

## Unknown command

```bash
uv run meridian nonexistent-command-xyz
```
- [ ] Exit non-zero

## Spawn dry-run baseline

```bash
uv run meridian spawn -a test -p "test prompt" --dry-run --json
```
- [ ] Exit 0
- [ ] `"status": "dry-run"` in JSON output
