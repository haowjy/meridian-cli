# Pi harness manual smoke gate

**Opt-in live tier:** these steps can invoke a paid provider. They are never part
of the default smoke or CI. Use an eligible cheap model, an explicit timeout,
and only the isolated stores created below.

This gate retains real argv, happy-path, failure-surfacing, native-path, and
run-boundary checks before the deeper [Pi RPC quiescence scenarios](pi-rpc-quiescence.md).

## Setup

Prepare dependencies first (network may be needed), then create exactly one
fixture in a fresh shell:

```bash
(
  cd src/meridian/pi_runtime
  pnpm install --frozen-lockfile
)
. tests/smoke/scripts/pi-setup.sh --build-extensions
pi --version
pi --help
```

Prerequisites:

- **Node 24+** on `PATH` (extension build / Pi toolchain; CI uses Node 24)
- **`pi` on `PATH`**, compatible with Meridian (`pi --version`, `pi --help` includes
  `--mode rpc` for spawned runs)
- **Provider auth:** setup intentionally does **not** inherit `~/.pi/agent`.
  Copy only files needed for this deliberate probe:
  `install -m 600 "$SMOKE_ORIGINAL_HOME/.pi/agent/auth.json" "$PI_CODING_AGENT_DIR/auth.json"`.
  Or supply the provider's API-key environment variable. Do not copy settings,
  extensions or the entire native agent tree.
- **Spawn session files:** `$PI_CODING_AGENT_SESSION_DIR/<spawn-id>/`
- **Meridian extension bundles:** `$MERIDIAN_PI_EXTENSION_INSTALL_ROOT` (the package
  `dist/extensions` tree); launches pass required bundle entrypoints with `-e`.

`pi-setup.sh` creates a fresh fixture with `MERIDIAN_HOME`,
`PI_CODING_AGENT_DIR` and `PI_CODING_AGENT_SESSION_DIR` below `SMOKE_ROOT`.
Managed launch preflight pins `_MERIDIAN_PI_STATE_DIR` to the project's runtime
under that isolated `MERIDIAN_HOME`; a separate caller-provided task-state root
is not honored. Do not point stores at the original home or a real project.
`SMOKE_ORIGINAL_HOME` is only a reference for deliberate auth copying.

Choose a cheap eligible Pi-native model from the live listing, rather than
assuming a hardcoded model remains available. `--live` may probe installed
providers; run it only after approving that cost/network boundary:

```bash
uv run meridian mars models list --harness pi --all --live
MODEL='<eligible-pi-model>'
```

## Runtime and argv gate

Before the happy path, inspect both launch shapes:

```bash
pi --version
pi --help
uv run meridian spawn --harness pi -m "$MODEL" \
  -p 'argv check' --dry-run --json
uv run meridian --harness pi -m "$MODEL" --dry-run --json
```

Expect:

- Spawned/RPC `cli_command` starts with `pi --mode rpc`; native primary starts
  with `pi` and does **not** contain `--mode rpc`.
- Both commands include `managed-bash`, `meridian-spawn-watch`, and
  `session-boundary` with default config. Managed-bash and spawn-watch respect
  `[harness.pi]` toggles; session-boundary stays loaded.
  The primary additionally includes `meridian-idle`; the spawned/RPC command
  must not.
- Spawned RPC includes `--no-extensions` unless `load_all_pi_extensions = true`;
  primary does not suppress native discovery. Both include `--session-dir` and
  an assigned `--session-id` for a fresh create.
- If `pi --help` lacks required RPC/extension flags, Meridian refuses launch and
  says to run `pi update` or set `MERIDIAN_PI_BINARY` to a compatible binary.

## Happy path

```bash
uv run meridian spawn --harness pi -m "$MODEL" -p 'Reply LIVE_OK' --timeout 5
```

Expect:

- Spawn status `succeeded`
- `report.md` contains a normal assistant reply (for example `LIVE_OK`), not
  lifecycle JSON
- `uv run meridian spawn show <spawn-id>` lists Pi runtime diagnostics and a
  terminal phase

## Failure surfacing (#262)

Provoke a failure with real Pi — for example an invalid model id or missing
provider auth — then inspect artifacts:

```bash
uv run meridian spawn --harness pi -m openai-codex/no-such-model -p 'hi' --timeout 1
# Or omit deliberately copied auth from PI_CODING_AGENT_DIR and retry a real
# model; never break the original ~/.pi/agent.
```

Expect:

- `state.json` status `failed` with a readable `error` / reason
- `report.md` (or spawn show) explains provider/auth/model failure with a
  readable `# Spawn failed` report
- Report body does **not** consist only of `cleanup_completed` lifecycle JSON

## pi_paths spot-check

After a successful spawn:

```bash
SPAWN_ID=<from create output>
ls -la "$PI_CODING_AGENT_SESSION_DIR/$SPAWN_ID/"
uv run meridian spawn show "$SPAWN_ID" --verbose
uv run meridian session log "$SPAWN_ID"
```

With the shared setup, expect native session JSONL under
`$PI_CODING_AGENT_SESSION_DIR/<spawn-id>`. Resume retains its recorded store.
The projected `-e` paths point to `$MERIDIAN_PI_EXTENSION_INSTALL_ROOT` bundles,
not per-launch copies in the Pi agent tree. Extension disk state uses the
project runtime through `_MERIDIAN_PI_STATE_DIR`; native transcript reads use
the recorded session key.

Inspect `run_boundary` in JSON spawn output: only a matching nonce/PID record
with a final readable quit verifies exit identity. Unresolved is valid when
shutdown cannot supply that evidence; never pick a recent journal as a
substitute.
