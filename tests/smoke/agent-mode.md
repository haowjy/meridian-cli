# Smoke: agent mode

Restricted help, output defaults, and flag behavior in agent mode.

## Setup

```bash
export SCRATCH=$(mktemp -d)
export MERIDIAN_HOME=$(mktemp -d)
export MERIDIAN_PROJECT_DIR=$SCRATCH
```

## Agent help is restricted

```bash
MERIDIAN_DEPTH=1 uv run meridian --help
```
- [ ] Exit 0
- [ ] `spawn`, `work`, and `models` appear in output
- [ ] At least one of `config`, `doctor`, `init` is absent (operator commands hidden)

## --human flag restores full help

```bash
MERIDIAN_DEPTH=1 uv run meridian --human --help
```
- [ ] Exit 0
- [ ] `spawn` and `work` appear in output

## --agent flag forces restricted help without nested-process detection

```bash
MERIDIAN_DEPTH= uv run meridian --agent --help
```
- [ ] Exit 0
- [ ] `spawn`, `work`, `models` appear in output
- [ ] `init` does NOT appear in output

## --agent and --human are mutually exclusive

```bash
MERIDIAN_DEPTH=1 uv run meridian --agent --human --help
```
- [ ] Exit 1
- [ ] stderr contains `Cannot combine --agent with --human`

## models list redirect works in agent mode

```bash
MERIDIAN_DEPTH=1 uv run meridian models list
```
- [ ] Exit 1
- [ ] stderr contains `meridian mars models list`
- [ ] stdout is empty

## models list redirect ignores --format json

```bash
MERIDIAN_DEPTH=1 uv run meridian --format json models list
```
- [ ] Exit 1
- [ ] stderr contains `meridian mars models list`
- [ ] stdout is empty

## Control-plane commands default to JSON in agent mode

```bash
MERIDIAN_DEPTH=1 uv run meridian work current
```
- [ ] If exit 0 and stdout is non-empty, stdout is valid JSON
