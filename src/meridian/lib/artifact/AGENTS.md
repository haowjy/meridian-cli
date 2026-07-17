# lib/artifact/ — Detached Artifact Serving

Thin wrapper that serves a static artifact directory over Tailscale. No daemon:
`meridian artifact serve` launches a detached stdlib HTTP server, registers a
Tailscale serve path, records state, and exits. `list`/`stop`/`gc` read and prune
that state. The CLI is the only entry point; nothing else imports this package.

## Mental model

```
artifact serve <dir>
  → launch_http_server():  _serve_dir on 127.0.0.1:<port>  (setsid, detached)
  → _tailscale_register():  tailscale serve --https=<port> --set-path /<slug> → 127.0.0.1:<port>
  → record Serve in ~/.meridian/serves.json, print URL, exit
```

The server outlives the CLI by design ("share and walk away"). Cleanup is TTL +
lazy GC on every `serve`/`gc`, and explicit `stop`. State is the file; there is no
in-memory handle between commands.

## Key invariants

- **Never serve on 443 or well-known ports.** The CLI rejects ports < 1024 and
  defaults to random 49152–65535; `_tailscale_register` always passes an explicit
  `--https=<port>`. Portless 443 does not route to local serve handlers on some
  nodes, and claiming well-known ports clobbers unrelated serves. See KB
  `research/tailscale-serve-semantics.md`.
- **Teardown is per-path only.** `tailscale_off` runs
  `tailscale serve --https=<port> --set-path=/<slug> off` — removes ONE slug.
  Never bare `--https=<port> off` (drops all slugs on the port) or `serve reset`
  (wipes the whole node). Only remove slugs recorded in serves.json.
- **`tailscale serve --set-path` forwards the full path.** `_serve_dir.py` strips
  the `/<slug>` prefix in `translate_path` so root-serving backends resolve. Strip
  in path translation, not URL rewriting, so directory redirects keep the prefix.
- **No parent-death linkage.** `launch_http_server` uses
  `start_new_session=True`. The legacy native-Windows branch requests
  `DETACHED_PROCESS` and is untested. Adding linkage breaks the feature: TTL +
  lazy GC *is* the cleanup mechanism.
- **Validate the slug at every trust boundary** before any Tailscale subprocess.
  A malformed/empty slug would produce `--set-path=/ off` and clobber a root
  handler. `is_valid_slug` runs at registration, teardown, URL build, and load.
- **State is atomic and locked.** `serves.json` writes go through
  `atomic_write_text`; reads tolerate truncation/missing; `start_serve`,
  `stop_serve`, and `sweep_expired` hold `serves.json.lock` across
  read-modify-save. `Serve.process_created_at` (birth epoch) guards PID-reuse
  from terminating an unrelated process tree.

## Anti-patterns

- Don't add a daemon or keep an in-memory handle between commands — the file is
  the authority and `list`/`stop`/`gc` run in fresh processes.
- Don't pick the local bind port independently of the Tailscale `--https` port;
  they are the same number, and it must avoid serves.json, live
  `tailscale serve status --json`, and an EADDRINUSE probe.
- `--funnel` exposes publicly on the fixed Funnel port **10000** (Funnel allows
  only 443/8443/10000; 443 is portless-broken and 8443 is commonly taken). The
  local backend keeps its own random/`--port` high port; `tailnet_port()` derives
  the public port from the funnel flag. If Funnel isn't enabled on the tailnet the
  register call fails loudly (ArtifactError) and rolls back — no orphan state.
- Don't reset/abort a GC sweep on a single terminate failure — each serve is
  independent; a failed teardown must preserve its entry, not stop the loop.

## Related

- [../../AGENTS.md](../../AGENTS.md) — package layers; this package is a leaf,
  not part of the ops/launch/state spine
- KB `research/tailscale-serve-semantics.md` — Tailscale routing/teardown facts
  behind the invariants above
