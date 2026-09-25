"""Shared pytest fixtures."""

import os
import sys
from pathlib import Path

import pytest
import structlog

from tests.support.git import is_git_env_key

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only test")
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only test")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "posix_only: test requires POSIX semantics")
    config.addinivalue_line("markers", "windows_only: test requires Windows semantics")
    config.addinivalue_line("markers", "unit: pure logic tests, no IO")
    config.addinivalue_line("markers", "integration: one real boundary")
    config.addinivalue_line("markers", "e2e: full CLI invocation")
    config.addinivalue_line("markers", "contract: parity/drift checks")
    config.addinivalue_line("markers", "slow: takes >1s")
    if config.getoption("--runner-history") == "off":
        blind_dir = PACKAGE_ROOT / "tests" / "support" / "runner_history_blind"
        os.environ["MERIDIAN_TEST_RUNNER_HISTORY"] = "off"
        existing = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = str(blind_dir) + (os.pathsep + existing if existing else "")


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--runner-history",
        choices=("on", "off"),
        default="on",
        help="Disable runner-history writers and trap reads (PR 2 deletion gate)",
    )


@pytest.fixture
def package_root() -> Path:
    return PACKAGE_ROOT


@pytest.fixture(autouse=True, scope="session")
def _isolate_meridian_home(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Set MERIDIAN_HOME for the full test session."""

    test_home = tmp_path_factory.mktemp("meridian-home")
    os.environ["MERIDIAN_HOME"] = str(test_home)


_NATIVE_STORE_ENV = frozenset({
    "CODEX_HOME",
    "CLAUDE_CONFIG_DIR",
    "OPENCODE_DB",
    "OPENCODE_CONFIG",
    "OPENCODE_CONFIG_DIR",
    "PI_CODING_AGENT_DIR",
    "PI_CODING_AGENT_SESSION_DIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
})


@pytest.fixture(autouse=True)
def _clean_meridian_runtime_env(
    monkeypatch: pytest.MonkeyPatch,
    _isolate_meridian_home: None,
) -> None:
    """Isolate tests from parent harness runtime state environment.

    Harness store variables are cleared too: a test that swaps HOME must not
    reach the user's real native stores through an inherited CODEX_HOME etc.
    """

    session_home = os.environ.get("MERIDIAN_HOME")
    for key in tuple(os.environ):
        if key.upper().startswith(("MERIDIAN_", "_MERIDIAN_")) or key in _NATIVE_STORE_ENV:
            monkeypatch.delenv(key, raising=False)

    if session_home is not None:
        monkeypatch.setenv("MERIDIAN_HOME", session_home)


@pytest.fixture(autouse=True)
def _runner_history_blind_mode(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if request.config.getoption("--runner-history") == "off":
        from tests.support.runner_history_blind.patches import install_runner_history_blind

        install_runner_history_blind(monkeypatch)


@pytest.fixture(autouse=True)
def _clean_git_runtime_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Isolate tests from parent git environment and host git config.

    Some integration tests shell out to real ``git`` in temporary repos. If a
    parent process leaks ``GIT_DIR`` / ``GIT_WORK_TREE`` / related overrides
    into pytest, those commands can silently target the real checkout instead
    of the temp repo. Strip inherited git env and point global config at a temp
    file so machine-local signing, aliases, or hooks cannot affect the suite.
    """

    for key in tuple(os.environ):
        if is_git_env_key(key):
            monkeypatch.delenv(key, raising=False)

    git_config = tmp_path / "gitconfig"
    git_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(git_config))


@pytest.fixture(autouse=True)
def _reset_structlog_state() -> None:
    """Reset structlog defaults between tests.

    CLI paths configure structlog with ``cache_logger_on_first_use=True``.
    Without a reset, cached logger/config state can leak across tests and make
    capture or stderr assertions depend on collection order.
    """

    structlog.reset_defaults()
    yield
    structlog.reset_defaults()


@pytest.fixture(autouse=True)
def _reset_process_telemetry_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset process-global telemetry state between tests.

    Several suites exercise lifecycle observers and process-wide telemetry
    routing in-process. Without an explicit reset, observers, per-spawn
    sequence counters, and background router threads can leak across tests and
    make order matter.
    """

    import meridian.lib.core.telemetry as core_telemetry
    import meridian.lib.telemetry.observers as telemetry_observers
    import meridian.lib.telemetry.router as telemetry_router

    existing_router = getattr(telemetry_router, "_global_router", None)
    if existing_router is not None:
        existing_router.close()
        telemetry_router._global_router = None

    monkeypatch.setattr(telemetry_observers, "_GLOBAL_OBSERVERS", [])
    monkeypatch.setattr(telemetry_observers, "_debug_trace_registered", False)
    monkeypatch.setattr(
        core_telemetry,
        "_GLOBAL_EVENT_COUNTER",
        core_telemetry.SpawnEventCounter(),
    )

    yield

    router = getattr(telemetry_router, "_global_router", None)
    if router is not None:
        router.close()
        telemetry_router._global_router = None
