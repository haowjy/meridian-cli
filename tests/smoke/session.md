# Smoke: session

Session log, search, and repair — error paths and help.

## Setup

```bash
. tests/smoke/scripts/setup.sh
```

## Invalid ref — all subcommands fail cleanly

```bash
uv run meridian session log invalid-ref-xyz-123
```
- [ ] Exit non-zero
- [ ] No `Traceback` in stderr

```bash
uv run meridian session search pattern invalid-ref-xyz-456
```
- [ ] Exit non-zero
- [ ] No `Traceback` in stderr

```bash
uv run meridian session repair invalid-ref-xyz-789
```
- [ ] Exit non-zero
- [ ] No `Traceback` in stderr

## Help — all subcommands show usage

```bash
uv run meridian session log --help
```
- [ ] Exit 0
- [ ] `log` or `session` appears in output (case-insensitive)
- [ ] `--tail` appears in output
- [ ] `--full` appears in output
- [ ] `--no-truncate` appears in output
- [ ] `--raw` appears in output
- [ ] `--segment` appears in output
- [ ] `--from` appears in output
- [ ] `--around` appears in output
- [ ] `--last` does not appear in output
- [ ] `--offset` does not appear in output

```bash
uv run meridian session search --help
```
- [ ] Exit 0
- [ ] `search` appears in output (case-insensitive)
- [ ] `--workspace` appears in output
- [ ] `--global` appears in output

```bash
uv run meridian session repair --help
```
- [ ] Exit 0
- [ ] `repair` appears in output (case-insensitive)

## Log readability — clean default, raw escape hatch, expanded tool output

Create a deterministic transcript with XML chrome and a tool call/result pair:

```bash
cat > "$SCRATCH/session-log-readable.jsonl" <<'JSONL'
{"type":"user","message":{"content":[{"type":"text","text":"<local-command-caveat>internal</local-command-caveat><bash-input>echo hi</bash-input><bash-stdout>hi</bash-stdout><system-reminder>noise</system-reminder>"}]}}
{"type":"assistant","message":{"content":[{"type":"text","text":"I'll run a command."}]}}
{"type":"assistant","message":{"content":[{"type":"tool_use","name":"bash","input":{"command":"printf hi"}}]}}
{"type":"user","message":{"content":[{"type":"tool_result","content":"<bash-stdout>hi</bash-stdout>"}]}}
JSONL
```

```bash
uv run meridian session log --file "$SCRATCH/session-log-readable.jsonl" --full
```
- [ ] Exit 0
- [ ] Output starts with a clean session header (`# Session`)
- [ ] Role headers look like `**System**` / `**Mixed**`, not raw segment metadata
- [ ] XML chrome is removed: no `<local-command-caveat>` or `<system-reminder>`
- [ ] Command input is readable (`$ echo hi`)
- [ ] Tool action is collapsed (`  $ printf hi`)
- [ ] Raw `[tool_result]` is not shown
- [ ] Footer hints mention `Use --no-truncate to expand tool outputs`

```bash
uv run meridian session log --file "$SCRATCH/session-log-readable.jsonl" --full --no-truncate
```
- [ ] Exit 0
- [ ] Tool output expands inline (`  hi`)
- [ ] `Use --no-truncate to expand tool outputs` is not shown

```bash
uv run meridian session log --file "$SCRATCH/session-log-readable.jsonl" --full --raw
```
- [ ] Exit 0
- [ ] Raw entry headers include segment/message metadata (`--- 1 [segment`)
- [ ] Raw transcript markers remain visible (`[tool_result]`, `<bash-stdout>hi</bash-stdout>`)
