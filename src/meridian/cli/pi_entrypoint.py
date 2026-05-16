"""Entrypoint for the ``meridian-pi`` wrapper binary."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from meridian.lib.state.user_paths import get_user_home

_WRAPPER_AGENT_DIR_FLAG = "--agent-dir"
_WRAPPER_AGENT_DIR_ENV = "MERIDIAN_PI_AGENT_DIR"
_WRAPPER_BINARY_ENV = "MERIDIAN_PI_BINARY"
_PI_AGENT_DIR_ENV = "PI_CODING_AGENT_DIR"
_NODE_BIN_ENV = "MERIDIAN_PI_NODE_BIN"
_PI_BINARY_NAME = "meridian-pi"
_REQUIRED_AGENT_SUBDIRECTORIES: tuple[str, ...] = ("sessions", "extensions", "bin")


def _strip_agent_dir_flag(argv: Sequence[str]) -> tuple[list[str], str | None]:
    """Strip wrapper-only ``--agent-dir`` flag and return passthrough args."""

    passthrough: list[str] = []
    cli_agent_dir: str | None = None

    i = 0
    while i < len(argv):
        token = argv[i]

        if token == _WRAPPER_AGENT_DIR_FLAG:
            next_index = i + 1
            if next_index >= len(argv):
                raise ValueError("--agent-dir requires a path value")
            candidate = argv[next_index].strip()
            if not candidate:
                raise ValueError("--agent-dir requires a non-empty path value")
            cli_agent_dir = candidate
            i += 2
            continue

        if token.startswith(f"{_WRAPPER_AGENT_DIR_FLAG}="):
            candidate = token.split("=", 1)[1].strip()
            if not candidate:
                raise ValueError("--agent-dir requires a non-empty path value")
            cli_agent_dir = candidate
            i += 1
            continue

        passthrough.append(token)
        i += 1

    return passthrough, cli_agent_dir


def _resolve_agent_dir(cli_agent_dir: str | None, env: Mapping[str, str]) -> Path:
    """Resolve ``PI_CODING_AGENT_DIR`` using CLI > env > default precedence."""

    if cli_agent_dir is not None:
        return Path(cli_agent_dir).expanduser()

    env_agent_dir = env.get(_WRAPPER_AGENT_DIR_ENV, "").strip()
    if env_agent_dir:
        return Path(env_agent_dir).expanduser()

    meridian_home = env.get("MERIDIAN_HOME", "").strip()
    if meridian_home:
        return Path(meridian_home).expanduser() / "pi" / "agent"

    return get_user_home() / "pi" / "agent"


def _ensure_agent_dir_layout(agent_dir: Path) -> None:
    """Create base Pi agent directory and required subdirectories."""

    agent_dir.mkdir(parents=True, exist_ok=True)
    for subdirectory in _REQUIRED_AGENT_SUBDIRECTORIES:
        (agent_dir / subdirectory).mkdir(parents=True, exist_ok=True)


def _runner_path() -> Path:
    """Return the Node runner path for SDK-backed Pi execution."""

    return Path(__file__).resolve().parents[1] / "pi_runtime" / "runner.mjs"


def _compiled_binary_candidates() -> tuple[Path, ...]:
    """Return candidate paths for the Bun-compiled Pi binary."""

    runtime_dir = Path(__file__).resolve().parents[1] / "pi_runtime" / "bin"
    return tuple(
        runtime_dir / candidate_name
        for candidate_name in _compiled_binary_candidate_names(os.name)
    )


def _compiled_binary_candidate_names(os_name: str) -> tuple[str, str]:
    """Return OS-ordered runtime binary candidate names."""

    base_name = _PI_BINARY_NAME
    windows_name = f"{_PI_BINARY_NAME}.exe"
    if os_name == "nt":
        return (windows_name, base_name)
    return (base_name, windows_name)


def _is_runnable_binary(candidate: Path) -> bool:
    """Return whether ``candidate`` is runnable on this host."""

    if not candidate.is_file():
        return False

    if os.name == "nt":
        return True

    return os.access(candidate, os.X_OK)


def _resolve_packaged_binary() -> Path | None:
    """Return the packaged Bun binary path when present and runnable."""

    for candidate in _compiled_binary_candidates():
        if _is_runnable_binary(candidate):
            return candidate
    return None


def _first_present_packaged_binary() -> Path | None:
    """Return the first packaged Bun binary candidate that exists on disk."""

    for candidate in _compiled_binary_candidates():
        if candidate.is_file():
            return candidate
    return None


def _build_child_env(base_env: Mapping[str, str], agent_dir: Path) -> dict[str, str]:
    """Build child environment with the resolved Pi agent dir."""

    child_env = dict(base_env)
    child_env[_PI_AGENT_DIR_ENV] = str(agent_dir)
    return child_env


def _node_fallback_available(runner_path: Path) -> bool:
    """Return whether source/dev Node fallback dependencies are available."""

    runtime_dependency_dir = (
        runner_path.parent / "node_modules" / "@earendil-works" / "pi-coding-agent"
    )
    return runner_path.is_file() and runtime_dependency_dir.is_dir()


def _print_error(message: str) -> None:
    print(f"meridian-pi: {message}", file=sys.stderr)


def main() -> None:
    """Launch Pi via SDK with Meridian config defaults."""

    try:
        passthrough_args, cli_agent_dir = _strip_agent_dir_flag(sys.argv[1:])
    except ValueError as error:
        _print_error(str(error))
        raise SystemExit(2) from error

    agent_dir = _resolve_agent_dir(cli_agent_dir, os.environ)

    try:
        _ensure_agent_dir_layout(agent_dir)
    except OSError as error:
        _print_error(f"failed to create agent directory '{agent_dir}': {error}")
        raise SystemExit(1) from error

    child_env = _build_child_env(os.environ, agent_dir)
    binary_override = child_env.get(_WRAPPER_BINARY_ENV, "").strip()
    used_node_fallback = False
    command_kind = "pi-runtime-binary"
    if binary_override:
        command = [str(Path(binary_override).expanduser()), *passthrough_args]
    else:
        packaged_binary = _resolve_packaged_binary()
        if packaged_binary is not None:
            command = [str(packaged_binary), *passthrough_args]
        else:
            present_packaged_binary = _first_present_packaged_binary()
            if present_packaged_binary is not None:
                _print_error(
                    "packaged Pi runtime binary is present but not executable on this host: "
                    f"{present_packaged_binary}"
                )
                raise SystemExit(1)
            runner_path = _runner_path()
            if not _node_fallback_available(runner_path):
                _print_error(
                    "compiled meridian-pi runtime is not installed; build it with "
                    "scripts/build-meridian-pi-runtime.sh or set MERIDIAN_PI_BINARY; "
                    "source/dev fallback requires runtime deps installed."
                )
                raise SystemExit(1)
            used_node_fallback = True
            command_kind = "node-runtime"
            node_bin = child_env.get(_NODE_BIN_ENV, "node").strip() or "node"
            command = [node_bin, str(runner_path), *passthrough_args]

    try:
        completed = subprocess.run(command, env=child_env, check=False)
    except FileNotFoundError as error:
        if used_node_fallback:
            _print_error(
                "Node.js runtime is required for meridian-pi (install Node.js 20+ and retry)."
            )
        else:
            _print_error(f"Pi runtime binary not found: {command[0]}")
        raise SystemExit(1) from error
    except OSError as error:
        if command_kind == "node-runtime":
            _print_error(f"failed to execute Node.js runtime command '{command[0]}': {error}")
        else:
            _print_error(f"failed to execute Pi runtime binary '{command[0]}': {error}")
        raise SystemExit(1) from error

    raise SystemExit(completed.returncode)


__all__ = [
    "_build_child_env",
    "_compiled_binary_candidate_names",
    "_compiled_binary_candidates",
    "_ensure_agent_dir_layout",
    "_first_present_packaged_binary",
    "_is_runnable_binary",
    "_node_fallback_available",
    "_resolve_agent_dir",
    "_resolve_packaged_binary",
    "_runner_path",
    "_strip_agent_dir_flag",
    "main",
]
