"""Shared pytest fixtures."""

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import structlog

from tests.support.git import is_git_env_key

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only test")
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only test")
_GIT_CONFIG_GUARD_SNAPSHOT: tuple[Path, bytes] | None = None


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "posix_only: test requires POSIX semantics")
    config.addinivalue_line("markers", "windows_only: test requires Windows semantics")
    config.addinivalue_line("markers", "unit: pure logic tests, no IO")
    config.addinivalue_line("markers", "integration: one real boundary")
    config.addinivalue_line("markers", "e2e: full CLI invocation")
    config.addinivalue_line("markers", "contract: parity/drift checks")
    config.addinivalue_line("markers", "slow: takes >1s")


def pytest_sessionstart(session: pytest.Session) -> None:
    """Snapshot checkout-local Git config in the xdist controller only."""

    if hasattr(session.config, "workerinput"):
        return

    git_config_path = _package_git_config_path()
    if git_config_path is None or not git_config_path.exists():
        return

    global _GIT_CONFIG_GUARD_SNAPSHOT
    _GIT_CONFIG_GUARD_SNAPSHOT = (git_config_path, git_config_path.read_bytes())


def pytest_sessionfinish(session: pytest.Session, exitstatus: int | pytest.ExitCode) -> None:
    """Restore and fail if tests mutated the checkout's local Git config."""

    if hasattr(session.config, "workerinput") or _GIT_CONFIG_GUARD_SNAPSHOT is None:
        return

    git_config_path, before = _GIT_CONFIG_GUARD_SNAPSHOT
    after = git_config_path.read_bytes() if git_config_path.exists() else None
    if after != before:
        git_config_path.parent.mkdir(parents=True, exist_ok=True)
        git_config_path.write_bytes(before)
        if exitstatus in (pytest.ExitCode.OK, pytest.ExitCode.NO_TESTS_COLLECTED):
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
        session.config._meridian_git_config_guard_message = (  # type: ignore[attr-defined]
            "Tests modified the repo-local Git config; restored "
            f"{git_config_path}. Run without xdist to isolate the leaking test."
        )


def pytest_terminal_summary(
    terminalreporter: Any,
    exitstatus: int | pytest.ExitCode,
    config: pytest.Config,
) -> None:
    message = getattr(config, "_meridian_git_config_guard_message", None)
    if message:
        terminalreporter.write_sep("=", "repo-local Git config guard")
        terminalreporter.write_line(message, red=True)


@pytest.fixture
def package_root() -> Path:
    return PACKAGE_ROOT


def _package_git_config_path() -> Path | None:
    env = {key: value for key, value in os.environ.items() if not is_git_env_key(key)}
    try:
        result = subprocess.run(
            ["git", "-C", str(PACKAGE_ROOT), "rev-parse", "--git-path", "config"],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    config_path = Path(result.stdout.strip())
    if not config_path.is_absolute():
        config_path = PACKAGE_ROOT / config_path
    return config_path


@pytest.fixture(autouse=True, scope="session")
def _isolate_meridian_home(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Set MERIDIAN_HOME for the full test session."""

    test_home = tmp_path_factory.mktemp("meridian-home")
    os.environ["MERIDIAN_HOME"] = str(test_home)


@pytest.fixture(autouse=True)
def _clean_meridian_runtime_env(
    monkeypatch: pytest.MonkeyPatch,
    _isolate_meridian_home: None,
) -> None:
    """Isolate tests from parent harness runtime state environment."""

    session_home = os.environ.get("MERIDIAN_HOME")
    for key in tuple(os.environ):
        if key.upper().startswith("MERIDIAN_"):
            monkeypatch.delenv(key, raising=False)

    if session_home is not None:
        monkeypatch.setenv("MERIDIAN_HOME", session_home)


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
