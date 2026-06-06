# Unit Tests

Pure logic, no real I/O. Parsers, state machines, policy resolution, formatters.

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
