# Spawn Return Report

Manual smoke checklist for foreground spawn and single-spawn wait output.

## Validation: spawn-return-report

Use an isolated scratch project and a cheap model/agent. Replace `pNNN` with the
spawn id printed by each command.

```bash
export MERIDIAN_HOME="$(mktemp -d)"
scratch="$(mktemp -d)"
cd "$scratch"
meridian init
```

1. Foreground default:
   ```bash
   meridian spawn -a meridian-subagent -m gpt-5.4-mini -p 'Reply with exactly OK'
   ```
   Expect compact text: one status line, report body, then
   `Transcript: meridian session log <spawn_id>`. No token/cost/path block.

2. Foreground metadata:
   ```bash
   meridian spawn -a meridian-subagent -m gpt-5.4-mini -p 'Reply with exactly OK' --metadata
   ```
   Expect report body plus inline accounting fields such as model, duration,
   tokens/cost when available, report path, and transcript command.

3. Foreground JSON:
   ```bash
   meridian --format json spawn -a meridian-subagent -m gpt-5.4-mini -p 'Reply with exactly OK'
   ```
   Expect valid JSON on stdout with `report` and `transcript_command`. No
   running-status preamble before the JSON object.

4. Agent-mode foreground default:
   ```bash
   MERIDIAN_DEPTH=1 meridian spawn -a meridian-subagent -m gpt-5.4-mini -p 'Reply with exactly OK'
   ```
   Expect compact text, not JSON.

5. Agent-mode background preservation:
   ```bash
   MERIDIAN_DEPTH=1 meridian spawn -a meridian-subagent -m gpt-5.4-mini -p 'Reply with exactly OK' --bg
   ```
   Expect JSON wait-note wire output with `wait_required: true`; no compact
   report view and no transcript pointer in the submission response.

6. Wait default:
   ```bash
   meridian spawn wait pNNN
   ```
   Expect compact text with report body and transcript command.

7. Wait no-report:
   ```bash
   meridian spawn wait pNNN --no-report
   ```
   Expect status and transcript command; report body omitted.

8. Wait JSON:
   ```bash
   meridian --format json spawn wait pNNN
   ```
   Expect JSON with `report_body` and `transcript_command`.

9. Multi-wait text, if two completed ids are available:
   ```bash
   meridian spawn wait pNNN pMMM
   ```
   Expect table plus `Report for <id>` sections; not the single-spawn compact
   status format.

10. Show/status progressive detail:
    ```bash
    meridian spawn show pNNN
    meridian spawn show pNNN --no-report
    meridian spawn status pNNN
    meridian spawn status pNNN --report
    meridian spawn status pNNN --verbose
    ```
    Expect `show` default text to include the moderate status/model/duration
    summary, report path, report body, and transcript command. Expect
    `show --no-report` and `status` to keep the summary/report path/transcript
    while omitting the report body. Expect `status --report` to add the report
    body. Expect `--verbose` to add internal diagnostics such as token/cost
    fields or harness/session metadata when available; those internals should
    not appear in the non-verbose `show`/`status` output.
