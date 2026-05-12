# dev_frontend — Contracts and Architecture

## Protocols

`launcher.py` defines three contracts all launchers and sessions must satisfy:

**`FrontendLauncher`** — factory for one dev session:
```python
def launch(self, frontend_root: Path, backend: BackendEndpoint) -> LaunchResult
```

**`FrontendSession`** — the running dev process:
- `url` — browser-facing URL (stable after launch)
- `wait_until_ready(timeout)` — awaitable; raises `RuntimeError` or `TimeoutError` on failure
- `poll()` — returns exit code if exited, `None` if still running
- `terminate(grace_period)` — terminates gracefully, escalates to kill

**`BackendEndpoint`** — backend address resolved from bind host/port:
- `http_origin` / `ws_origin` — passed to Vite as `VITE_API_PROXY_TARGET` / `VITE_WS_PROXY_TARGET`
- `client_host` — maps wildcard bind hosts (`0.0.0.0`, `::`) to `127.0.0.1` for client use

`BackendEndpoint` is constructed by `DevSupervisor` from `bind_host` + `port` before launching
the frontend. `FrontendLauncher.launch()` receives it and uses it to configure Vite's proxy target.

## Transport Selection

`resolve_dev_frontend_launcher()` in `policy.py` enforces:
- `--no-portless` + (`--tailscale` or `--funnel`) → `DevFrontendConfigurationError`
- `--tailscale` + `--funnel` → `DevFrontendConfigurationError`
- `--tailscale`/`--funnel` without portless available → `DevFrontendConfigurationError`
- `--portless-force` without effective portless mode → `DevFrontendConfigurationError`

Default: portless if the `portless` executable is on PATH, raw otherwise.
`--no-portless` forces raw even when portless is available.

## Portless Collision Handling

`PortlessLauncher` uses a 2-second exit window to distinguish route collision from clean startup:
- If portless exits within 2 seconds with a route-occupied stderr pattern →
  `PortlessRouteOccupiedError` with actionable fix instructions (`portless prune`, `--portless-force`)
- If it exits within 2 seconds for another reason → `FrontendLaunchError` with stderr
- If it survives 2 seconds → process is considered running; URL is fetched from `portless get`

`--portless-force` passes `--force` to the portless command to take over an occupied route.

## DevSupervisor Lifecycle

`DevSupervisor.run()`:
1. Starts uvicorn backend in an asyncio task
2. Waits for uvicorn to report `started`
3. Creates an initial chat via POST `/chat` — chat ID is appended to the frontend URL as `?chat_id=`
4. Calls `launcher.launch()`, then `session.wait_until_ready(timeout=30)`
5. Opens browser if `--open`
6. Monitors: waits for SIGINT/SIGTERM, backend task exit, or unexpected frontend exit

On shutdown (any trigger): signals uvicorn to exit, terminates the frontend session.

## Env Sanitization

`PortlessLauncher` strips `PORTLESS_*`, `VITE_API_URL`, and `VITE_WS_URL` from the child env before
injecting new values. This prevents ambient dev routing config from leaking into the child process.

## Uplinks

→ [KB: architecture/chat/dev-frontend.md](/home/jimyao/.meridian/git/meridian-flow-docs/kb/architecture/chat/dev-frontend.md)

## Lateral Links

→ [../.context/CONTEXT.md](../.context/CONTEXT.md) — chat layer; this module runs alongside the FastAPI chat app
