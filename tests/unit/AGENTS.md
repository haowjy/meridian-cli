# Unit Tests

Pure functional cores: parsers, state machines, policy resolution, and
formatters. No real filesystem, subprocess, network, or OS behavior.

A coherent security suite may stay here when light in-process dry-run
composition and temporary config scaffolding are necessary to prove one
fail-closed boundary. Keep the suite whole; this is a narrow security
exception, not permission for general wiring tests.

Mirror `src/meridian/lib/` structure under `tests/unit/`.

## Red Flags (Move to Integration)

- `monkeypatch.setattr(module.subprocess, "run", ...)`
- `monkeypatch.setattr(module, "_private_thing", ...)`
- Test takes >100ms
- Uses `tmp_path` for non-trivial file operations

## Running

```bash
uv run pytest tests/unit/ -v           # All
uv run pytest tests/unit/ -k "mars"    # Pattern
```

Should complete in <2 seconds total.
