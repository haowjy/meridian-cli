# chat/dev_frontend/

Dev frontend management for `meridian chat --dev`. Launches and supervises a
Vite dev server alongside the backend, selecting a transport (portless or raw)
based on CLI flags.

## Entry Points

- `__init__.py` — `resolve_dev_frontend_root()`, `validate_dev_prerequisites()`; public surface
- `policy.py` — `resolve_dev_frontend_launcher()`: validates flag combinations, builds launcher
- `supervisor.py` — `DevSupervisor.run()`: runs backend (uvicorn) + frontend together until shutdown

## Key Files

- `launcher.py` — protocols: `FrontendLauncher`, `FrontendSession`, `BackendEndpoint`
- `portless.py` — `PortlessLauncher` / `PortlessSession`: portless-managed HTTPS route
- `raw_vite.py` — `RawViteLauncher`: direct Vite dev server on a local port
- `discovery.py` — probe portless availability and Tailscale DNS

## Architecture

```
resolve_dev_frontend_launcher()   ← validates flags, builds launcher
        │
        ▼
DevSupervisor.run()               ← uvicorn (backend) + FrontendLauncher.launch()
        │
        ├── PortlessLauncher.launch()   ← portless-managed HTTPS
        └── RawViteLauncher.launch()    ← direct Vite dev server
```

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — protocol contracts, portless collision handling,
  transport selection rules

## Related

→ [../.context/CONTEXT.md](../.context/CONTEXT.md) — parent chat layer context
