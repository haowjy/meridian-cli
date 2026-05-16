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
_PI_AGENT_DIR_ENV = "PI_CODING_AGENT_DIR"
_NODE_BIN_ENV = "MERIDIAN_PI_NODE_BIN"
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


def _build_child_env(base_env: Mapping[str, str], agent_dir: Path) -> dict[str, str]:
    """Build child environment with the resolved Pi agent dir."""

    child_env = dict(base_env)
    child_env[_PI_AGENT_DIR_ENV] = str(agent_dir)
    return child_env


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
    node_bin = child_env.get(_NODE_BIN_ENV, "node").strip() or "node"
    runner_path = _runner_path()
    command = [node_bin, str(runner_path), *passthrough_args]

    try:
        completed = subprocess.run(command, env=child_env, check=False)
    except FileNotFoundError as error:
        _print_error(
            "Node.js runtime is required for meridian-pi (install Node.js 20+ and retry)."
        )
        raise SystemExit(1) from error

    raise SystemExit(completed.returncode)


__all__ = [
    "_build_child_env",
    "_ensure_agent_dir_layout",
    "_resolve_agent_dir",
    "_runner_path",
    "_strip_agent_dir_flag",
    "main",
]
