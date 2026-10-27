# lib/artifact/ — Context

## Flow

```
artifact serve <dir>
  → launch_http_server():  _serve_dir on 127.0.0.1:<port>  (setsid, detached)
  → _tailscale_register():  tailscale serve --https=<port> --set-path /<slug> → 127.0.0.1:<port>
  → record Serve in ~/.meridian/serves.json, print URL, exit
```

The server outlives the CLI by design ("share and walk away"). Cleanup is TTL plus lazy GC
on every `serve`/`gc`, and explicit `stop`. State is the file; no in-memory handle between
commands. The CLI is the only entry point; nothing else imports this package.

## Why the rules in AGENTS.md

- Portless 443 does not route to local serve handlers on some nodes, and claiming
  well-known ports clobbers unrelated serves. See KB `research/tailscale-serve-semantics.md`.
- `--funnel` uses the fixed Funnel port 10000 (Funnel allows only 443/8443/10000; 443 is
  portless-broken, 8443 commonly taken). The local backend keeps its own random/`--port`
  high port; `tailnet_port()` derives the public port. If Funnel isn't enabled on the
  tailnet registration fails loudly (`ArtifactError`) and rolls back, leaving no orphan state.
- `tailscale serve --set-path` forwards the full path; `_serve_dir.py` strips the `/<slug>`
  prefix in `translate_path` (not URL rewriting) so directory redirects keep the prefix.
- A malformed/empty slug would produce `--set-path=/ off` and clobber a root handler.
