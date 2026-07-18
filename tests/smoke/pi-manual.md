# qa-validated: pi-manual

# Pi harness manual smoke gate

Short checklist before deeper Pi RPC scenarios (`pi-rpc-quiescence.md`). Use a **real**
installed `pi` on `PATH` — not a stub script or `MERIDIAN_PI_BINARY` pointed at a fake
binary.

For the nested local-source parent/child quiescence check, use
`pi-rpc-quiescence.md` S6d after this gate passes.

## Setup

```bash
. tests/smoke/scripts/pi-setup.sh --build-extensions
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
  default launches use `--no-extensions` plus explicit `-e` to those bundles only

Optional: `. tests/smoke/scripts/pi-setup.sh --isolated-state` sets
`_MERIDIAN_PI_STATE_DIR` to a temp dir for extension disk state only.

Use a cheap model (e.g. `openai-codex/gpt-5.4-mini`) for plumbing checks.

---

## Happy path

```bash
meridian spawn --harness pi -m openai-codex/gpt-5.4-mini -p 'Reply LIVE_OK'
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
ls -la ~/.pi/agent/extensions/meridian/
ls -la ~/.meridian/meridian-pi/sessions/"$SPAWN_ID"/
meridian spawn show "$SPAWN_ID"
```

Expect per-spawn extension materialization under `extensions/meridian/<launch-id>/` and
session JSONL under `meridian-pi/sessions/<spawn-id>/`.
