"""Shared pytest fixtures."""

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
import structlog

from tests.support.git import is_git_env_key
from tests.support.machine_ids import plant_id_claims

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PI_DIST_ROOT = PACKAGE_ROOT / "src" / "meridian" / "pi_runtime" / "dist"
PI_DIST_BUILD_COMMAND = "cd src/meridian/pi_runtime && pnpm install && pnpm run build:extensions"
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only test")
requires_pi_dist = pytest.mark.requires_pi_dist


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "posix_only: test requires POSIX semantics")
    config.addinivalue_line("markers", "unit: pure logic tests, no IO")
    config.addinivalue_line("markers", "integration: one real boundary")
    config.addinivalue_line("markers", "e2e: full CLI invocation")
    config.addinivalue_line("markers", "contract: parity/drift checks")
    config.addinivalue_line("markers", "slow: takes >1s")
    config.addinivalue_line("markers", "requires_pi_dist: test requires built Pi extension bundles")
    if config.getoption("--runner-history") == "off":
        blind_dir = PACKAGE_ROOT / "tests" / "support" / "runner_history_blind"
        existing = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = str(blind_dir) + (os.pathsep + existing if existing else "")


def pytest_runtest_call(item: pytest.Item) -> None:
    if item.get_closest_marker("requires_pi_dist") is None or PI_DIST_ROOT.is_dir():
        return

    message = (
        f"Pi extension bundles are missing at {PI_DIST_ROOT}; build them with "
        f"`{PI_DIST_BUILD_COMMAND}`"
    )
    if os.environ.get("GITHUB_ACTIONS") == "true":
        pytest.fail(message, pytrace=False)
    pytest.skip(message)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--runner-history",
        choices=("on", "off"),
        default="on",
        help="Trap implicit reads of runner history.jsonl, in-process and in subprocesses",
    )


@pytest.fixture
def package_root() -> Path:
    return PACKAGE_ROOT


@pytest.fixture(autouse=True, scope="session")
def _isolate_meridian_home(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Set MERIDIAN_HOME for the full test session."""

    test_home = tmp_path_factory.mktemp("meridian-home")
    os.environ["MERIDIAN_HOME"] = str(test_home)


@pytest.fixture(autouse=True, scope="session")
def _isolate_control_sockets() -> Iterator[Path]:
    from meridian.lib.streaming import control_socket

    production_root = control_socket._POSIX_SOCKET_ROOT
    # Tests that SIGKILL a runner leave endpoints behind; keep them out of the user's
    # real /tmp/meridian-<uid>. The root stays under /tmp so socket paths stay short.
    with (
        TemporaryDirectory(prefix="meridian-test-", dir="/tmp") as root,
        pytest.MonkeyPatch.context() as patch,
    ):
        patch.setattr(control_socket, "_POSIX_SOCKET_ROOT", Path(root))
        # Path-contract tests can opt back into the unmodified production default.
        yield production_root


# A harness session sets its own variables (`CLAUDECODE`, `CLAUDE_CODE_SESSION_ID`, the
# `CLAUDE_CODE_PROMPT_CACHE_TTL` Meridian gave a spawned Claude), so a run from inside one
# would read them as the environment a launch inherits.
_HARNESS_ENV_PREFIXES = ("CLAUDE", "CODEX_", "OPENCODE_", "PI_CODING_AGENT_")
_NATIVE_STORE_ENV = frozenset({"XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"})


@pytest.fixture(autouse=True)
def _isolate_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Point HOME at an empty per-test directory.

    Native harness stores default under HOME (``~/.claude``, ``~/.codex``,
    ``~/.local/share/opencode``), so without this a test reads whatever native config
    the caller has and pass/fail depends on their machine. A test that needs native
    config sets HOME (or the store variable) itself.
    """

    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))


@pytest.fixture(autouse=True)
def _clean_meridian_runtime_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
    _isolate_meridian_home: None,
) -> None:
    """Isolate tests from parent harness runtime state environment.

    Harness variables are cleared too: a test that swaps HOME must not reach the
    user's real native stores through an inherited CODEX_HOME etc., and a run from
    inside a harness session must not pass that session's variables to a launch.
    A real `mars` gets an empty user config and cache, since the developer's
    `~/.mars/config.toml` is a config layer (agents, models, settings).
    """

    for key in tuple(os.environ):
        if (
            key.upper().startswith(("MERIDIAN_", "_MERIDIAN_", *_HARNESS_ENV_PREFIXES))
            or key in _NATIVE_STORE_ENV
        ):
            monkeypatch.delenv(key, raising=False)

    # Issued IDs are machine-wide state under the home: a fresh home per test keeps
    # every test's numbering independent and never touches the real ~/.meridian.
    home = tmp_path_factory.mktemp("meridian-home")
    monkeypatch.setenv("MERIDIAN_HOME", str(home))
    plant_id_claims(home)

    mars_user = tmp_path_factory.mktemp("mars-user")
    monkeypatch.setenv("MARS_HOME", str(mars_user / "home"))
    monkeypatch.setenv("MARS_CACHE_DIR", str(mars_user / "cache"))


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
