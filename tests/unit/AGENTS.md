# Unit Tests

See [tests/AGENTS.md](../AGENTS.md) for the full testing guide.

## Unit Test Specifics

Unit tests verify **pure logic** with **no real I/O**:
- Parsers, transformers, validators
- State machines and reducers  
- Policy/precedence resolution
- Formatters and serializers

### Available Fakes

From `tests/support/fakes.py`:
- `FakeClock` - controllable time
- `FakeHeartbeat` - heartbeat backend stub

Prefer real v2 state helpers (`create_lifecycle_service`, `spawn_store.get_spawn`,
or direct `state.json` assertions) for spawn state tests. Avoid new in-memory
spawn repository fakes outside narrow lifecycle dependency-injection tests.

### Red Flags (Move to Integration)

- `monkeypatch.setattr(module.subprocess, "run", ...)`
- `monkeypatch.setattr(module, "_private_thing", ...)`
- Test takes >100ms
- Uses `tmp_path` for non-trivial file operations

### File Organization

Mirror `src/meridian/lib/` structure:
```
tests/unit/
├── catalog/    # Model catalog logic
├── config/     # Config parsing/resolution
├── harness/    # Harness projections, extraction
├── launch/     # Launch context, policies
├── ops/        # Operation helpers
└── state/      # State machine, lifecycle
```

### Running

```bash
uv run pytest tests/unit/ -v           # All
uv run pytest tests/unit/ -k "mars"    # Pattern
```

Unit tests should complete in <2 seconds total.
