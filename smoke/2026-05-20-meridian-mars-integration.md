# Meridian ↔ Mars integration smoke — 2026-05-20

Worktree: `/home/jimyao/gitrepos/meridian-cli.worktrees/mars-capability-cache-resolver`

Binary under test: `uv run meridian`

Mars in this env: `uv run mars --version` → `mars 0.4.6`

Method note: for scenario A I used `--dry-run --json` for routing probes so I could capture the exact harness CLI command without burning unnecessary paid tokens. Real OpenCode runs were used in scenario D.

## A. Unified routing — `meridian spawn`

### A1. Claude-family aliases route to Claude

Command (exit 0):

```bash
cd /home/jimyao/gitrepos/meridian-cli.worktrees/mars-capability-cache-resolver && \
uv run meridian spawn -m haiku -p 'Reply with exactly OK' --dry-run --json
```

Key output:

```json
{"harness_id":"claude","model":"claude-haiku-4-5","model_selection":{"requested_token":"haiku","canonical_model_id":"claude-haiku-4-5","harness_provenance":"mars-provided"}}
```

Verdict: PASS.

Command (exit 0):

```bash
uv run meridian spawn -m sonnet -p 'Reply with exactly OK' --dry-run --json
```

Key output:

```json
{"harness_id":"claude","model":"claude-sonnet-4-6","model_selection":{"requested_token":"sonnet","canonical_model_id":"claude-sonnet-4-6","harness_provenance":"mars-provided"}}
```

Verdict: PASS.

Command (exit 0):

```bash
uv run meridian spawn -m opus47 -p 'Reply with exactly OK' --dry-run --json
```

Key output:

```json
{"harness_id":"claude","model":"claude-opus-4-7","model_selection":{"requested_token":"opus47","canonical_model_id":"claude-opus-4-7","harness_provenance":"mars-provided"}}
```

Verdict: PASS.

### A2. OpenAI-family aliases route to Codex on the default PATH

Command (exit 0):

```bash
uv run meridian spawn -m gpt-5.4-mini -p 'Reply with exactly OK' --dry-run --json
```

Key output:

```json
{"harness_id":"codex","model":"gpt-5.4-mini","cli_command":["codex","exec","--json","--model","gpt-5.4-mini",...],"model_selection":{"requested_token":"gpt-5.4-mini","canonical_model_id":"gpt-5.4-mini","harness_provenance":"mars-provided"}}
```

Verdict: PASS for default Codex routing.

Command (exit 0):

```bash
uv run meridian spawn -m gpt55 -p 'Reply with exactly OK' --dry-run --json
```

Key output:

```json
{"harness_id":"codex","model":"gpt-5.5","cli_command":["codex","exec","--json","--model","gpt-5.5",...],"model_selection":{"requested_token":"gpt55","canonical_model_id":"gpt-5.5","harness_provenance":"mars-provided"}}
```

Verdict: PASS for default Codex routing.

Command (exit 0):

```bash
uv run meridian spawn -m codex -p 'Reply with exactly OK' --dry-run --json
```

Key output:

```json
{"harness_id":"codex","model":"gpt-5.3-codex","model_selection":{"requested_token":"codex","canonical_model_id":"gpt-5.3-codex","harness_provenance":"mars-provided"}}
```

Verdict: PASS.

### A3. Forced OpenCode routing does **not** pass Mars's runnable model ID through

Mars source-of-truth for the same env (exit 0):

```bash
PATH=/home/jimyao/.opencode/bin:/usr/bin:/bin /home/jimyao/.local/bin/uv run mars models resolve gpt-5.5 --json
```

Key output:

```json
{"harness":"opencode","probe_cache":"stale","runnable_paths":[{"harness":"opencode","harness_model_id":"openai/gpt-5.5","mars_provider":"openai"}]}
```

Meridian dry-run in the same env (exit 0):

```bash
PATH=/home/jimyao/.opencode/bin:/usr/bin:/bin /home/jimyao/.local/bin/uv run meridian spawn -m gpt-5.5 -p 'Reply with exactly OK' --dry-run --json
```

Key output:

```json
{"harness_id":"opencode","model":"gpt-5.5","cli_command":["opencode","run","--model","gpt-5.5","--variant","high","-"]}
```

Meridian dry-run for `gpt-5.4-mini` in the same env (exit 0):

```bash
PATH=/home/jimyao/.opencode/bin:/usr/bin:/bin /home/jimyao/.local/bin/uv run meridian spawn -m gpt-5.4-mini -p 'Reply with exactly OK' --dry-run --json
```

Key output:

```json
{"harness_id":"opencode","model":"gpt-5.4-mini","cli_command":["opencode","run","--model","gpt-5.4-mini","--variant","high","-"]}
```

Verdict: FAIL. Mars says the runnable OpenCode ID is `openai/gpt-5.5` / `openai/gpt-5.4-mini`; Meridian passes the canonical/raw ID instead.

### A4. CLI model override beats profile harness override

Command (exit 0):

```bash
TMP=$(mktemp -d)
mkdir -p "$TMP/.mars/agents"
cat > "$TMP/mars.toml" <<'TOML'
[settings]
targets = [".claude"]
TOML
cat > "$TMP/.mars/agents/reviewer.md" <<'EOF_AGENT'
---
name: reviewer
description: harness precedence smoke
model: sonnet
harness: claude
---
# Reviewer
Reply with exactly OK
EOF_AGENT
MERIDIAN_PROJECT_DIR="$TMP" MERIDIAN_RUNTIME_DIR="$TMP/.meridian" \
uv run meridian spawn -a reviewer -m gpt-5.4-mini -p 'Reply with exactly OK' --dry-run --json
```

Key output:

```json
{"agent":"reviewer","harness_id":"codex","model":"gpt-5.4-mini","model_selection":{"requested_token":"gpt-5.4-mini","canonical_model_id":"gpt-5.4-mini","harness_provenance":"model-derived-override"}}
```

Verdict: PASS. CLI model override correctly pulled routing away from the profile's `harness: claude`.

## B. Ad-hoc launch bundles

Command (exit 2):

```bash
cd /home/jimyao/gitrepos/meridian-cli.worktrees/mars-capability-cache-resolver && \
uv run meridian mars build launch-bundle --model gpt-5.5 --harness opencode --json
```

Key output:

```text
error: unrecognized subcommand 'build'
Usage: mars [OPTIONS] <COMMAND>
```

Direct `mars` behaved the same for every requested harness (`claude`, `codex`, `opencode`, `cursor`, `pi`) (exit 2 each):

```bash
for H in claude codex opencode cursor pi; do
  uv run sh -lc "mars build launch-bundle --model gpt-5.5 --harness $H --json"
done
```

Representative output:

```text
error: unrecognized subcommand 'build'
Usage: mars [OPTIONS] <COMMAND>
```

Verdict: FAIL. The Mars in this worktree (`0.4.6`) does not expose `build launch-bundle`, so Meridian cannot be consuming the new ad-hoc bundle surface end-to-end.

## C. Capability cache + `MARS_CACHE_DIR`

### C1. Meridian honors `MARS_CACHE_DIR`

Command sequence (exit 0):

```bash
CACHE_ROOT=$(mktemp -d)
FILE="$CACHE_ROOT/availability/opencode-probe.json"
PATH=/home/jimyao/.opencode/bin:/usr/bin:/bin MARS_CACHE_DIR="$CACHE_ROOT" \
  /home/jimyao/.local/bin/uv run meridian spawn -m gpt-5.5 -p 'Reply with exactly OK' --dry-run --json >/dev/null
MTIME1=$(stat -c %Y "$FILE")
sleep 2
PATH=/home/jimyao/.opencode/bin:/usr/bin:/bin MARS_CACHE_DIR="$CACHE_ROOT" \
  /home/jimyao/.local/bin/uv run meridian spawn -m gpt-5.5 -p 'Reply with exactly OK' --dry-run --json >/dev/null
MTIME2=$(stat -c %Y "$FILE")
PATH=/home/jimyao/.opencode/bin:/usr/bin:/bin MARS_CACHE_DIR="$CACHE_ROOT" \
  /home/jimyao/.local/bin/uv run mars models resolve gpt-5.5 --json
```

Key output:

```text
CACHE_ROOT=/tmp/tmp.eC4E6OcILu
MTIME1=1779292883
MTIME2=1779292883
```

```json
{"harness":"opencode","probe_cache":"hit","runnable_paths":[{"harness":"opencode","harness_model_id":"openai/gpt-5.5"}]}
```

Cache tree after the runs:

```text
/tmp/tmp.eC4E6OcILu/availability/.opencode-probe.lock
/tmp/tmp.eC4E6OcILu/availability/opencode-probe.json
```

Also checked for writes outside the temp cache after a baseline timestamp:

```text
(no files under ~/.mars/cache were newer than the baseline)
```

Verdict: PASS. Meridian's Mars-backed model resolution honored `MARS_CACHE_DIR`; the second dry-run reused the same probe cache file.

### C2. Linux `XDG_CACHE_HOME` fallback works when `MARS_CACHE_DIR` is unset

Command (exit 0):

```bash
XDG_CACHE_HOME=$(mktemp -d) \
PATH=/home/jimyao/.opencode/bin:/usr/bin:/bin \
/home/jimyao/.local/bin/uv run meridian spawn -m gpt-5.5 -p 'Reply with exactly OK' --dry-run --json >/dev/null
find "$XDG_CACHE_HOME" -maxdepth 4 -type f | sort
```

Key output:

```text
/tmp/tmp.3Ye5jaTzjk/mars/cache/availability/.opencode-probe.lock
/tmp/tmp.3Ye5jaTzjk/mars/cache/availability/opencode-probe.json
```

Verdict: PASS on Linux.

## D. OpenCode “not finalizing” / session issues

### D1. Fresh OpenCode-routed spawn succeeds, but the extracted report is wrong

Command (exit 0):

```bash
PATH=/home/jimyao/.opencode/bin:/usr/bin:/bin /home/jimyao/.local/bin/uv run meridian --json spawn -m gpt-5.5 -p 'Reply with exactly OK' --bg --timeout 1
sleep 10
PATH=/home/jimyao/.opencode/bin:/usr/bin:/bin /home/jimyao/.local/bin/uv run meridian --json spawn show p1754 --no-report
```

Key output:

```json
{"spawn_id":"p1754","status":"succeeded","harness":"opencode","model":"gpt-5.5","duration_secs":9.94,"exit_code":0}
```

`state.json` recorded the OpenCode session id:

```json
{"harness_session_id":"ses_1b9e4d174ffeWYJnalrkcMZ50A","status":"succeeded"}
```

But the extracted report was just the idle event:

```bash
sed -n '1,40p' /home/jimyao/.meridian/projects/mellow-hollow-brood/spawns/p1754/report.md
```

```text
# Auto-extracted Report

{"id":"evt_e461b51c4002cD3o2svtTg2C0s","properties":{"sessionID":"ses_1b9e4d174ffeWYJnalrkcMZ50A"},"type":"session.idle"}
```

Verdict: WEIRD. Fresh launch finalized successfully, but report extraction is still broken.

### D2. Actual OpenCode session metadata shows the wrong underlying model

From the same successful `p1754` run:

```bash
rg -n '"modelID"|"providerID"' /home/jimyao/.meridian/projects/mellow-hollow-brood/spawns/p1754/history.jsonl | tail -n 4
```

Key output:

```json
{"modelID":"deepseek-v4-pro","providerID":"opencode-go","sessionID":"ses_1b9e4d174ffeWYJnalrkcMZ50A"}
```

Verdict: FAIL. Meridian launched OpenCode with `--model gpt-5.5`, but the actual session recorded `deepseek-v4-pro`. This is consistent with Meridian not using Mars's harness-specific runnable ID.

### D3. Forced runner death leaves stale on-disk state

Command sequence (temp runtime, exit 0):

```bash
RUNTIME=$(mktemp -d)
MERIDIAN_RUNTIME_DIR="$RUNTIME" \
PATH=/home/jimyao/.opencode/bin:/usr/bin:/bin /home/jimyao/.local/bin/uv run meridian --json spawn -m gpt-5.5 -p 'Reply with exactly OK' --bg --timeout 1
# parsed spawn_id=p1, then killed runner pid 316561 almost immediately
sleep 3
MERIDIAN_RUNTIME_DIR="$RUNTIME" /home/jimyao/.local/bin/uv run meridian --json spawn show p1 --no-report
sed -n '1,220p' "$RUNTIME/spawns/p1/state.json"
```

Key output after the kill:

```json
{"spawn_id":"p1","status":"running","harness":"opencode","model":"gpt-5.5"}
```

```json
{"id":"p1","status":"running","runner_pid":316561,"worker_pid":null,"harness_session_id":null}
```

Later, CLI status surfaced a synthetic stale failure:

```bash
sleep 20
MERIDIAN_RUNTIME_DIR="$RUNTIME" /home/jimyao/.local/bin/uv run meridian --json spawn show p1 --no-report
```

```json
{"spawn_id":"p1","status":"failed","failure_reason":"stale_nested_read","exit_code":1}
```

Verdict: WEIRD. Not the exact old “queued forever” symptom, but a stale-state problem remains: the on-disk row stayed `running` after the runner died, and the CLI only recovered via synthetic stale-read failure.

### D4. Transcript rendering is not empty anymore

Commands (exit 0):

```bash
PATH=/home/jimyao/.opencode/bin:/usr/bin:/bin /home/jimyao/.local/bin/uv run meridian session log p1754
PATH=/home/jimyao/.opencode/bin:/usr/bin:/bin /home/jimyao/.local/bin/uv run meridian session log ses_1b9e4d174ffeWYJnalrkcMZ50A
```

Key output in both cases:

```text
Session ses_1b9e4d174ffeWYJnalrkcMZ50A (opencode transcript) — segment 0, 2 messages
--- 1 [user] ---
Reply with exactly OK
--- 2 [assistant] ---
OK
```

Verdict: PASS for transcript rendering.

### D5. Raw `ses_*` continue is still broken, and spawn-id continue hangs

Raw session-id continue (exit 1):

```bash
PATH=/home/jimyao/.opencode/bin:/usr/bin:/bin /home/jimyao/.local/bin/uv run meridian --json spawn --continue ses_1b9e4d174ffeWYJnalrkcMZ50A -m gpt-5.5 -p 'Reply with exactly SECOND' --bg --timeout 1
```

Key output:

```json
{"error":"Spawn 'ses_1b9e4d174ffeWYJnalrkcMZ50A' not found","exit_code":1}
```

Spawn-id continue (exit 0 to submit, then hung until cancelled):

```bash
PATH=/home/jimyao/.opencode/bin:/usr/bin:/bin /home/jimyao/.local/bin/uv run meridian --json spawn --continue p1754 -m gpt-5.5 -p 'Reply with exactly AGAIN' --bg --timeout 1
sleep 25
PATH=/home/jimyao/.opencode/bin:/usr/bin:/bin /home/jimyao/.local/bin/uv run meridian --json spawn show p1755 --no-report
ps -p 324884 -o pid=,stat=,etime=,cmd=
PATH=/home/jimyao/.opencode/bin:/usr/bin:/bin /home/jimyao/.local/bin/uv run meridian spawn cancel p1755
```

Key output before cancellation:

```json
{"spawn_id":"p1755","status":"running","harness":"opencode","model":"gpt-5.5"}
```

```text
324884 SNsl 00:50 /home/jimyao/gitrepos/meridian-cli.worktrees/mars-capability-cache-resolver/.venv/bin/python3 -m meridian.lib.ops.spawn.execute_bg --spawn-id p1755 --project-root /home/jimyao/gitrepos/meridian-cli
```

Cancellation result:

```json
{"spawn_id":"p1755","status":"cancelled","exit_code":143}
```

Verdict: FAIL/WEIRD. Raw `ses_*` continue is unsupported, and spawn-id continue on the OpenCode session hung indefinitely in my run.

### Bug status summary

| Bug | Status | Evidence |
|---|---|---|
| #230 OpenCode startup hang / queued indefinitely | PARTIAL / DIFFERENT FAILURE | Fresh `p1754` succeeded in 9.94s, so I did **not** repro the original simple-startup hang. But `--continue p1754` created `p1755`, which sat `running` until I cancelled it. |
| #236 foreground dies mid-launch + remains queued; report extraction stores `session.idle` JSON | PARTIAL / REPRODUCED IN DIFFERENT FORM | I reproduced the bad report extraction on a **successful** run (`report.md` = raw `session.idle` JSON). Forced runner death left stale `running` state, later surfaced as `stale_nested_read`, not queued. |
| #212 recorded sessions empty; raw `ses_*` continue needs harness metadata | MIXED | Session rendering is **fixed** (`session log` showed `OK`). Raw `ses_*` continue is still broken (`Spawn 'ses_*' not found`). Spawn-id continue also hung. |

## E. Quick regression sweep

### E1. Ruff

Command (exit 2):

```bash
cd /home/jimyao/gitrepos/meridian-cli.worktrees/mars-capability-cache-resolver && uv run ruff check .
```

Key output:

```text
error: Failed to spawn: `ruff`
  Caused by: No such file or directory (os error 2)
```

Verdict: FAIL/BLOCKED. The worktree env does not currently have `ruff` installed.

### E2. Pyright

Command (exit 1):

```bash
uv run pyright
```

Key output:

```text
Traceback (most recent call last):
  File ".../.venv/bin/pyright", line 4, in <module>
    from pyright.cli import entrypoint
ModuleNotFoundError: No module named 'pyright'
```

Verdict: FAIL/BLOCKED. `pyright` is not installed in the active env.

### E3. pytest-llm

Command (exit 1):

```bash
uv run pytest-llm
```

Key output:

```text
FAILED tests/integration/chat/test_acquisition.py::test_cold_acquisition_registers_observer_before_start_and_uses_persistent_policy
async def functions are not natively supported.
You need to install a suitable plugin for your async framework, for example:
  - anyio
  - pytest-asyncio
```

Summary:

```text
1 failed, 68 passed, 123 warnings in 4.35s
```

Verdict: FAIL/BLOCKED. The env is missing async pytest support.

## F. Alignment / structural friction

### F1. Current Meridian source is still on the legacy local resolution path

Command:

```bash
cd /home/jimyao/gitrepos/meridian-cli.worktrees/mars-capability-cache-resolver && \
rg -n "compile_launch_params|CompilerRequest|select_harness_model_id|run_mars_models_resolve|run_mars_models_list" \
  src/meridian/lib/launch/policies.py \
  src/meridian/lib/catalog/model_aliases.py \
  src/meridian/lib/launch/resolve.py \
  src/meridian/lib/launch/compiler.py \
  src/meridian/lib/ops -S
```

Key output:

```text
src/meridian/lib/launch/compiler.py:50:class CompilerRequest:
src/meridian/lib/launch/compiler.py:171:def compile_launch_params(request: CompilerRequest) -> CompilerResult:
src/meridian/lib/launch/resolve.py:210:def select_harness_model_id(
src/meridian/lib/catalog/model_aliases.py:400:def run_mars_models_resolve(
src/meridian/lib/launch/policies.py:24:    CompilerRequest,
src/meridian/lib/launch/policies.py:28:    compile_launch_params,
src/meridian/lib/launch/policies.py:45:    select_harness_model_id,
src/meridian/lib/launch/policies.py:528:    compiler_request = CompilerRequest(
```

Command:

```bash
rg -n "launch-bundle|launch_bundle|build launch-bundle|MarsLaunchBundle" src/meridian -S
```

Key output:

```text
(no matches)
```

Verdict: FAIL vs the design docs. Meridian still shells out to `mars models resolve/list` for model identity, but launch policy/runnable-ID shaping remains local. The ad-hoc bundle consumer described in `design/adhoc-launch-bundles-and-pi.md` is not present in this tree.

### F2. Concrete drift from `design/unified-model-routing.md`

Observed drift:

1. Mars advertises OpenCode runnable paths like `openai/gpt-5.5`, but Meridian sends raw `gpt-5.5` / `gpt-5.4-mini`.
2. The actual OpenCode session for `p1754` recorded `modelID: deepseek-v4-pro`, which is strong end-to-end evidence that the runnable-ID drift is user-visible at runtime.
3. `meridian mars build launch-bundle` / `mars build launch-bundle` is unavailable in the active env, so the canonical bundle-driven path cannot be exercised here at all.
4. `MARS_CACHE_DIR` / `XDG_CACHE_HOME` are honored for the Mars-backed OpenCode probe cache; that slice does align.

## Open Questions

1. The worktree's `uv run mars --version` is `0.4.6`, and that binary has no `build` subcommand. Is this branch expected to have a newer Mars dependency pinned, or is the released Mars surface still outside this Python package line?
2. The successful OpenCode run `p1754` reported `model: gpt-5.5` at the Meridian layer, but `history.jsonl` recorded actual `modelID: deepseek-v4-pro`. Is OpenCode silently defaulting on unknown model IDs, or is Meridian dropping/misprojecting a provider-qualified model somewhere else before launch?
3. The `--continue p1754` hang (`p1755`) looks different from the original startup-hang report. Is the supported continuation contract for OpenCode supposed to be spawn-id only, raw `ses_*`, both, or neither right now?
4. The regression sweep failures came from missing dev tools/plugins in the active env rather than obvious source breakage. Should the verification environment first run `uv sync --extra dev`, or is this env drift itself considered a release problem?

## Post-bump probes (mars-agents 0.4.8rc3)

Date: 2026-05-20  
Worktree: `/home/jimyao/gitrepos/meridian-cli.worktrees/mars-capability-cache-resolver`  
Binary under test: `uv run meridian`

### P1. Dependency bump + mars surface

Commands (exit 0):

```bash
uv lock --upgrade-package mars-agents
uv sync --extra dev
uv run meridian mars --version
uv run meridian mars build launch-bundle --help
uv run mars build launch-bundle --help
```

Key output:

```text
Updated mars-agents v0.4.6 -> v0.4.8rc3
...
Installed 2 packages in 1ms
- mars-agents==0.4.6
+ mars-agents==0.4.8rc3
...
mars 0.4.8-rc.3
Build a harness-targeted launch scaffold/bundle for an agent or ad-hoc launch
Usage: mars build launch-bundle [OPTIONS]
```

Verdict: PASS. `build launch-bundle` is now present through both `meridian mars` and direct `mars`.

### P2. Ad-hoc launch-bundle probes by harness

Note: `mars build launch-bundle` accepts `--model` (not `-m`) in `0.4.8rc3`; `-m` returns `unexpected argument '-m'`.

Commands (all exit 0):

```bash
uv run meridian mars build launch-bundle --harness opencode --model gpt-5.5 --json
uv run meridian mars build launch-bundle --harness claude --model haiku --json
uv run meridian mars build launch-bundle --harness codex --model gpt-5.4-mini --json
uv run meridian mars build launch-bundle --harness cursor --model gpt-5.4-mini --json
uv run meridian mars build launch-bundle --harness pi --model gpt-5.4-mini --json
```

Key routing output:

```text
opencode: harness_model=openai/gpt-5.5, harness_model_source=cached-probe, confidence=confirmed
claude:   harness_model=claude-haiku-4-5, harness_model_source=provider-match, confidence=likely
codex:    harness_model=gpt-5.4-mini, harness_model_source=provider-match, confidence=likely
cursor:   harness_model=gpt-5.4-mini, harness_model_source=passthrough, confidence=unknown
pi:       harness_model=gpt-5.4-mini, harness_model_source=passthrough, confidence=unknown
```

Alias nuance for OpenCode:

```bash
uv run meridian mars build launch-bundle --harness opencode --model gpt55 --json
```

```text
routing.harness_model=gpt55
routing.harness_model_source=passthrough
routing.harness_model_confidence=unknown
```

Verdict: PASS for bundle surface availability and per-harness JSON output. OpenCode provider-qualified runnable ID is confirmed when using `--model gpt-5.5`; alias token `gpt55` stays passthrough.

### P3. `MARS_CACHE_DIR` sanity

Command (exit 0):

```bash
CACHE_DIR=$(mktemp -d)
MARS_CACHE_DIR="$CACHE_DIR" \
  uv run meridian mars build launch-bundle --harness opencode --model gpt-5.5 --json
find "$CACHE_DIR" -maxdepth 4 -type f | sort
```

Key output:

```text
/tmp/tmp.PfGxFe16Gm/availability/.opencode-probe.lock
/tmp/tmp.PfGxFe16Gm/availability/.pi.lock
/tmp/tmp.PfGxFe16Gm/availability/opencode-probe.json
/tmp/tmp.PfGxFe16Gm/availability/pi.json
```

Verdict: PASS. Cache/probe files landed under the override root.

### P4. Regression sweep after bump

Commands:

```bash
uv run ruff check .
uv run pyright
uv run pytest-llm
```

Results:

```text
ruff: PASS (All checks passed!)
pyright: PASS (0 errors, 0 warnings, 0 informations)
pytest-llm: FAIL (1 failed, 475 passed)
  failing test: tests/integration/launch/test_launch_resolution_runtime.py::test_launch_policy_terminal_surface_mode_defaults_to_pty_mediated[codex]
  assertion: expected HarnessId.CODEX, got HarnessId.CLAUDE
```

Triage: The pytest failure appeared after the Mars bump and is in launch resolution/runtime behavior; needs follow-up as potentially bump-related routing drift.
