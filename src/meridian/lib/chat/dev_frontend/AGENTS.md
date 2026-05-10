# chat/dev_frontend/ — Dev Frontend Supervisor for `meridian chat --dev`

Launches and supervises a Vite dev server alongside the chat backend. Only active in dev mode — not used in production.

## Mental Model

`resolve_dev_frontend_launcher()` validates flag combinations and selects a transport (portless HTTPS or raw Vite). `DevSupervisor.run()` then manages both backend (uvicorn) and frontend together, shutting down cleanly when either exits.

```
resolve_dev_frontend_launcher()   ← validates flags, selects transport
        │
        ▼
DevSupervisor.run()               ← runs uvicorn (backend) + FrontendLauncher.launch()
        ├── PortlessLauncher      ← portless-managed HTTPS (default when portless available)
        └── RawViteLauncher       ← direct Vite on a local port (fallback or --raw-vite)
```

## Entry Points

- `__init__.py` — `resolve_dev_frontend_root()`, `validate_dev_prerequisites()`
- `policy.py` — `resolve_dev_frontend_launcher()`: flag validation and launcher construction
- `supervisor.py` — `DevSupervisor.run()`: process supervision loop

## Key Files

- `launcher.py` — `FrontendLauncher`, `FrontendSession`, `BackendEndpoint` protocols
- `portless.py` — `PortlessLauncher`/`PortlessSession`: portless-managed HTTPS route
- `raw_vite.py` — `RawViteLauncher`: direct Vite dev server on a local port
- `discovery.py` — probes portless availability and Tailscale DNS

## Key Rules

- Use Makefile targets (`make frontend`, `make backend`), not raw port numbers. Portless routes through `.localhost` domains — raw ports aren't the intended dev workflow.
- In git worktrees, portless auto-prefixes URLs with the branch name, allowing multiple worktrees to run simultaneously without collisions.

→ [.context/CONTEXT.md](.context/CONTEXT.md) — protocol contracts, portless collision handling, transport selection rules
→ [../.context/CONTEXT.md](../.context/CONTEXT.md) — parent chat layer
