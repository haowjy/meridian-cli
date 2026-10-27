# lib/artifact/ — Detached Artifact Serving

`meridian artifact serve` launches a detached stdlib HTTP server, registers a Tailscale
serve path, records state in `~/.meridian/serves.json`, and exits. `list`/`stop`/`gc` read
and prune that state. No daemon, no in-memory handle between commands: the file is the
authority. Flow and rationale: [.context/CONTEXT.md](.context/CONTEXT.md).

## Invariants

- **Never serve on 443 or well-known ports.** The CLI rejects ports < 1024 and defaults to
  random 49152–65535; `_tailscale_register` always passes explicit `--https=<port>`. The
  local bind port and the Tailscale `--https` port are the same number and must avoid
  serves.json, live `tailscale serve status --json`, and an EADDRINUSE probe. `--funnel`
  uses fixed port 10000.
- **Teardown is per-path only**: `tailscale serve --https=<port> --set-path=/<slug> off`.
  Never bare `--https=<port> off` (drops all slugs on the port) or `serve reset`. Only
  remove slugs recorded in serves.json.
- **Validate the slug at every trust boundary** (`is_valid_slug`: registration, teardown,
  URL build, load) before any Tailscale subprocess.
- **No parent-death linkage** (`start_new_session=True`): TTL plus lazy GC is the cleanup
  mechanism.
- **State is atomic and locked**: `serves.json` via `atomic_write_text`, reads tolerate
  truncation; `start_serve`, `stop_serve`, `sweep_expired` hold `serves.json.lock` across
  read-modify-save (lock rules: [../state/AGENTS.md](../state/AGENTS.md)).
  `Serve.process_created_at` guards PID reuse from terminating an unrelated tree.
- **A GC sweep continues past a single terminate failure**; a failed teardown preserves its
  entry.
