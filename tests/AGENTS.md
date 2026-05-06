# Testing Guide

Where to put tests, what they should look like, and when to write which kind.

## Research Foundation

This guide draws from:

| Source | Key Contribution |
|--------|------------------|
| **Kent Beck** — [Test Desiderata](https://testdesiderata.com/) | Properties of good tests |
| **Gary Bernhardt** — [Boundaries](https://www.destroyallsoftware.com/talks/boundaries) | Functional core / imperative shell |
| **J.B. Rainsberger** — [Integrated Tests Are A Scam](https://www.infoq.com/presentations/integration-tests-scam/) | Why integration tests lie |
| **Martin Fowler** — [Mocks Aren't Stubs](https://martinfowler.com/articles/mocksArentStubs.html) | Classicist vs mockist testing |
| **Michael Feathers** — Seams | Finding test injection points |
| **DHH** — [Test-Induced Design Damage](https://dhh.dk/2014/test-induced-design-damage.html) | When testing abstractions go too far |

## Test Desiderata (Kent Beck)

All good tests share these properties:

| Property | Meaning |
|----------|---------|
| **Isolated** | Tests don't affect each other; run in any order |
| **Deterministic** | Same inputs → same results, every time |
| **Fast** | Unit: milliseconds. Integration: seconds. Never minutes. |
| **Writable** | Easy to add new tests |
| **Readable** | Clear what's being tested and why |
| **Behavioral** | Test what code does, not how it does it |
| **Structure-insensitive** | Refactoring internals shouldn't break tests |
| **Predictive** | Test failure means production failure |

Tests that pin internal implementation details (exact call counts, private method names, internal data structures) fail **behavioral** and **structure-insensitive** — they become maintenance burdens that break on harmless refactors.

## Core Principles

### 1. Functional Core, Imperative Shell (Bernhardt)

Structure code so pure logic is separable from I/O:

```python
# BAD: Logic mixed with I/O - hard to unit test
def process_config(path: Path) -> Config:
    data = path.read_text()           # I/O (shell)
    parsed = json.loads(data)         # Pure (core)
    validated = validate(parsed)       # Pure (core)
    return Config(**validated)         # Pure (core)

# GOOD: Separate concerns
def parse_and_validate(data: str) -> Config:  # Pure core - unit test this
    parsed = json.loads(data)
    validated = validate(parsed)
    return Config(**validated)

def load_config(path: Path) -> Config:  # Thin shell - integration test this
    return parse_and_validate(path.read_text())
```

**The core** contains all branching logic, is pure, and is trivially unit-testable.
**The shell** does I/O and composition, has minimal branching, and gets integration-tested.

### 2. Classicist Baseline, Mockist at Boundaries (Fowler)

- Use **real collaborators** when they're cheap and deterministic
- Use **mocks/fakes** only at architectural seams (filesystem, network, time, external services)
- Never mock the thing you're testing

```python
# GOOD: Real collaborator (it's just a data class)
def test_format_output():
    record = SpawnRecord(id="p1", status="running")  # Real object
    assert format_record(record) == "p1: running"

# GOOD: Fake at boundary (time is non-deterministic)
def test_heartbeat_expires():
    clock = FakeClock(now=1000)
    heartbeat = Heartbeat(clock=clock)
    clock.advance(seconds=60)
    assert heartbeat.is_expired()

# BAD: Mocking internal collaborators
def test_foo():
    mock_helper = Mock()
    foo(helper=mock_helper)  # Now testing wiring, not behavior
```

### 3. No Test-Induced Design Damage (DHH)

Every abstraction must justify **runtime/business value**, not just testability.

```python
# BAD: Abstraction only exists for testing
class IStringFormatter(Protocol):  # Overkill for one implementation
    def format(self, s: str) -> str: ...

# GOOD: Abstraction has runtime value
class Clock(Protocol):  # Multiple real uses: RealClock, FakeClock for tests,
    def now(self) -> float: ...   # future: MockableClock for debugging
```

If you're adding an interface just to make something testable, reconsider. Maybe the code needs restructuring (extract pure logic), not more abstraction.

### 4. Integrated Tests Are A Scam (Rainsberger)

Integration tests that cross too many boundaries:
- Are slow
- Have combinatorial explosion of paths
- Give false confidence (pass but production fails)
- Give false failures (fail but production works)

**Solution:** Test each boundary once, in isolation. Then have a few true end-to-end tests for critical paths.

## The Two Rules

### 1. Test Behavior, Not Implementation

```python
# BAD: Tests HOW it works (breaks on refactor)
mock_helper.assert_called_once_with(internal_arg)
assert obj._private_field == expected
monkeypatch.setattr(module, "_internal_constant", value)

# GOOD: Tests WHAT it does (survives refactor)
assert result.status == "success"
assert "expected_output" in result.text
assert parse(input) == expected_output
```

### 2. One Test Per Invariant

Each test should verify ONE behavioral invariant. If a test name has "and" in it, consider splitting.

## Test Tiers

### Decision Tree

```
Does it touch filesystem, network, subprocess, or real external systems?
├─ NO → unit/
│       Pure function? Deterministic? Fast? → unit/
│
└─ YES → Does it verify a contract/schema that must not drift?
         ├─ YES → contract/
         └─ NO → Is it platform-specific (Windows vs POSIX)?
                 ├─ YES → platform/
                 └─ NO → integration/
```

### Unit Tests (`tests/unit/`)

**What:** Pure logic with no real I/O. Millisecond-fast.

**Shape:**
```python
def test_parse_extracts_version():
    result = parse_version("v1.2.3")
    assert result == Version(1, 2, 3)
```

**Injectable seams (Feathers):** When pure separation isn't possible, inject dependencies:
```python
def check_status(spawn_id: str, *, clock: Clock = real_clock) -> Status:
    ...

def test_check_status_expired():
    fake_clock = FakeClock(now=1000)
    result = check_status("p1", clock=fake_clock)
    assert result == Status.EXPIRED
```

Available fakes in `tests/support/fakes.py`: `FakeClock`, `FakeHeartbeat`.
For spawn state, prefer v2 state helpers (`create_lifecycle_service`,
`spawn_store.get_spawn`) or direct `state.json` assertions.

**Red flags that it's not a unit test:**
- `monkeypatch.setattr(module.subprocess, "run", ...)`
- `monkeypatch.setattr(module, "_private_thing", ...)`
- Test takes >100ms

### Integration Tests (`tests/integration/`)

**What:** Real I/O boundaries - filesystem, subprocesses, cross-module wiring.

**Shape:**
```python
def test_spawn_creates_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", str(tmp_path))
    start_spawn("p1", agent="test")
    assert (tmp_path / "spawns" / "p1").exists()
```

**Red flags that it's misclassified:**
- All I/O is mocked → move to unit
- Only testing one function's return value → probably unit

### Contract Tests (`tests/contract/`)

**What:** API shapes, type contracts, protocol parity that MUST NOT DRIFT.

Use sparingly. Only for things that would break downstream consumers.

### Platform Tests (`tests/platform/`)

**What:** OS-specific behavior (Windows vs POSIX).

```python
@pytest.mark.posix_only
def test_flock_blocks_concurrent_access(): ...

@pytest.mark.windows_only
def test_msvcrt_locking(): ...
```

### E2E / Smoke Tests (`tests/e2e/`)

**What:** Manual smoke test guides (markdown). These ARE the smoke tests.

Run before releases to verify full user flows.

## Anti-Patterns

### 1. Fake Integration Tests
```python
# This is a unit test wearing integration clothes
def test_check_status(monkeypatch):
    monkeypatch.setattr(subprocess, "run", fake_run)  # No real subprocess!
    ...
```
Move to `unit/` and use proper dependency injection.

### 2. Implementation Pinning
```python
monkeypatch.setattr(module, "_helper", mock)
mock.assert_called_with(exact_internal_args)  # Breaks on refactor
```

### 3. Over-Testing
```python
# 10 tests for the same invariant - consolidate!
@pytest.mark.parametrize("bad_input", ["", None, "  ", ...])
def test_rejects_invalid(bad_input): ...
```

### 4. Redundant Coverage
If unit and integration test the same path, keep the unit test.

### 5. Testing Framework Behavior
```python
# BAD: Testing that pytest/pydantic/fastapi works
def test_pydantic_validates_fields(): ...  # Pydantic already tests this
```

## Running Tests

```bash
uv run pytest tests/unit/ -v          # Fast, run often
uv run pytest tests/integration/ -v   # Before commit
uv run pytest tests/ -v               # Full suite before push
uv run pytest tests/ -k "spawn"       # Pattern match
```

## Test Quality Checklist

Before adding a test, ask:

- [ ] Tests behavior, not implementation?
- [ ] Survives internal refactoring?
- [ ] Right tier (unit/integration/contract)?
- [ ] No redundant coverage?
- [ ] Deterministic and isolated?
- [ ] Justifies its existence? (Not testing framework/library behavior)
