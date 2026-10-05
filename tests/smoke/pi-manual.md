# Pi harness manual smoke gate

Short checklist before deeper Pi RPC scenarios (`pi-rpc-quiescence.md`). Use a **real**
installed `pi` on `PATH` — not a stub script or `MERIDIAN_PI_BINARY` pointed at a fake
binary.

For the nested local-source parent/child quiescence check, use
the nested-spawn scenario in [pi-rpc-quiescence.md](pi-rpc-quiescence.md)
after this gate passes.

## Setup

```bash
(cd src/meridian/pi_runtime && pnpm install --frozen-lockfile && pnpm run build:extensions)
pi --version
pi --help
```

Prerequisites:

- **Node 24+** on `PATH` (extension build / Pi toolchain; CI uses Node 24)
- **`pi` on `PATH`**, compatible with Meridian (`pi --version`, `pi --help` includes
  `--mode rpc` for spawned runs)
- **Provider auth** under `~/.pi/agent` (Pi's agent tree). Meridian sets
  `PI_CODING_AGENT_DIR` to that directory (or your override) for subprocess launches.
- **Spawn session files:** `~/.meridian/meridian-pi/sessions/<spawn-id>/` (or under
  `MERIDIAN_HOME` when set)
- **Meridian extension bundles:** `~/.meridian/pi/extensions/` (or package `dist/extensions`);
  Meridian launches pass the required bundle entrypoints explicitly with `-e`

For isolated managed runs, set `MERIDIAN_HOME` to a temporary directory. Prelaunch
pins `_MERIDIAN_PI_STATE_DIR` to that project's runtime; it does not honor a
separate temporary task-state root.

Choose a cheap eligible Pi-native model from the live listing, rather than assuming
a hardcoded model remains available:

```bash
meridian mars models list --harness pi --all --live
MODEL='<eligible-pi-model>'
```

---

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

- Spawned/RPC `cli_command` starts with `pi --mode rpc`; the native primary command
  starts with `pi` and does **not** contain `--mode rpc`.
- Both commands include `managed-bash`, `meridian-spawn-watch`, and
  `session-boundary` with default config. Managed-bash and spawn-watch respect
  their `[harness.pi]` toggles; session-boundary stays loaded.
- Spawned RPC includes `--no-extensions` unless `load_all_pi_extensions = true`;
  primary does not suppress native discovery. Both include `--session-dir` and
  an assigned `--session-id` for a fresh create.
- If `pi --help` lacks the required RPC/extension flags, Meridian refuses the launch
  and says to run `pi update` or set `MERIDIAN_PI_BINARY` to a compatible Pi binary.

---

## Happy path

```bash
meridian spawn --harness pi -m "$MODEL" -p 'Reply LIVE_OK'
```

Expect:

- Spawn status `succeeded`
- `report.md` contains a normal assistant reply (e.g. includes `LIVE_OK`), not lifecycle
  JSON
- `meridian spawn show <spawn-id>` lists Pi runtime diagnostics and a terminal phase

---

## Failure surfacing (#262)

Provoke a failure with **real** Pi — for example an invalid model id or missing provider
auth — then inspect artifacts:

```bash
meridian spawn --harness pi -m openai-codex/no-such-model -p 'hi'
# or: temporarily break auth under ~/.pi/agent and retry a real model
```

Expect:

- `state.json` status `failed` with a readable `error` / reason
- `report.md` (or spawn show) explains the provider/auth/model failure with a readable `# Spawn failed` report
- Report body does **not** consist only of `cleanup_completed` lifecycle JSON

---

## pi_paths spot-check

After a successful spawn:

```bash
SPAWN_ID=<from create output>
ls -la ~/.meridian/meridian-pi/sessions/"$SPAWN_ID"/
meridian spawn show "$SPAWN_ID" --verbose
meridian session log "$SPAWN_ID"
```

With default paths, expect native session JSONL under
`~/.meridian/meridian-pi/sessions/<spawn-id>/`. Resume retains its recorded store.
The projected `-e` paths point to package `dist/extensions/` bundles or the stable
`~/.meridian/pi/extensions/` install root, not per-launch copies in the Pi agent
tree. Extension disk state uses the project runtime through
`_MERIDIAN_PI_STATE_DIR`; native transcript reads use the recorded session key.
Inspect `run_boundary` in JSON spawn output: only a matching nonce/PID record with
a final readable quit verifies exit identity. Unresolved is valid when shutdown
cannot supply that evidence; never pick a recent journal as a substitute.
