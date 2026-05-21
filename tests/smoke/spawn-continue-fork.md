# Smoke: non-Pi continue/fork flow

Background spawn + continue + fork + wait + transcript/report path.

## Setup

```bash
. tests/smoke/scripts/setup.sh
smoke_add_agent reviewer
```

## 1) Seed background spawn

```bash
uv run meridian spawn -a reviewer -m gpt-5.4-mini -p "Reply with exactly: SEED" --bg --json
```
- [ ] Exit 0
- [ ] JSON has `status == "running"`
- [ ] JSON has `spawn_id` (call this `SEED_ID`)

## 2) Continue from seed spawn

```bash
uv run meridian spawn --continue SEED_ID -p "Reply with exactly: CONTINUE" --bg --json
```
- [ ] Exit 0
- [ ] JSON has `status == "running"`
- [ ] JSON has `spawn_id` (call this `CONTINUE_ID`)

## 3) Fork from seed spawn

```bash
uv run meridian spawn --fork SEED_ID -p "Reply with exactly: FORK" --bg --json
```
- [ ] Exit 0
- [ ] JSON has `status == "running"`
- [ ] JSON has `spawn_id` (call this `FORK_ID`)

## 4) Wait for completions

```bash
uv run meridian spawn wait CONTINUE_ID FORK_ID --json
```
- [ ] Exit 0
- [ ] `total_runs == 2`
- [ ] both spawns end in terminal status (`succeeded`/`failed`/`cancelled`)

## 5) Transcript/report commands exist and work

```bash
uv run meridian session log CONTINUE_ID
uv run meridian spawn report show CONTINUE_ID
uv run meridian session log FORK_ID
uv run meridian spawn report show FORK_ID
```
- [ ] Each command exits 0
- [ ] `session log` returns conversation text (non-empty)
- [ ] `spawn report show` returns report text or explicit no-report message
