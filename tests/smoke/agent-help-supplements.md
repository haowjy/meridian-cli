# Smoke: agent help supplements

Agent-mode subcommand help text includes agent-specific notes and safe search placeholders.

## Setup

```bash
. tests/smoke/scripts/setup.sh
```

## spawn --help in agent mode includes agent notes

```bash
MERIDIAN_DEPTH=1 uv run meridian spawn --help
```
- [ ] Exit 0
- [ ] `Agent Notes:` appears in output
- [ ] `Lifecycle: queued` appears in output
- [ ] `Which subcommand when:` appears in output
- [ ] `session log` appears in output

## session --help uses renderer-safe search placeholders

```bash
MERIDIAN_DEPTH=1 uv run meridian session --help
```
- [ ] Exit 0
- [ ] `search QUERY REF` appears in output
- [ ] `meridian session search <query> <ref>` does NOT appear (angle-bracket form avoided)

## doctor --help in agent mode includes agent notes

```bash
MERIDIAN_DEPTH=1 uv run meridian doctor --help
```
- [ ] Exit 0
- [ ] `Agent Notes:` appears in output
- [ ] `read paths` appears in output
- [ ] `show, list, wait` appears in output
- [ ] `meridian session log SPAWN_ID` appears in output

## --human flag suppresses agent notes

```bash
MERIDIAN_DEPTH=1 uv run meridian --human spawn --help
```
- [ ] Exit 0
- [ ] `Agent Notes:` does NOT appear in output

## Agent notes restore correctly between invocations

```bash
MERIDIAN_DEPTH= uv run meridian --agent config --help
```
- [ ] Exit 0
- [ ] `Agent Notes:` appears in output
- [ ] `Resolution is per field` appears in output

```bash
MERIDIAN_DEPTH=1 uv run meridian --human config --help
```
- [ ] Exit 0
- [ ] `Agent Notes:` does NOT appear in output
