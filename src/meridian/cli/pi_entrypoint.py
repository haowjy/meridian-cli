"""Entrypoint for the ``meridian-pi`` wrapper binary."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from meridian.lib.launch.constants import PI_WRAPPER_METADATA_PATH_ENV
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.user_paths import get_user_home

logger = logging.getLogger(__name__)

_WRAPPER_AGENT_DIR_FLAG = "--agent-dir"
_WRAPPER_AGENT_DIR_ENV = "MERIDIAN_PI_AGENT_DIR"
_WRAPPER_BINARY_ENV = "MERIDIAN_PI_BINARY"
_WRAPPER_ALLOW_BUNDLED_FALLBACK_ENV = "MERIDIAN_PI_ALLOW_BUNDLED_FALLBACK"
_WRAPPER_BUNDLED_AUTH_CONFIRMED_ENV = "MERIDIAN_PI_BUNDLED_AUTH_CONFIRMED"
_PI_SESSION_ROLE_ENV = "MERIDIAN_PI_SESSION_ROLE"
_PI_AGENT_DIR_ENV = "PI_CODING_AGENT_DIR"
_PI_SESSION_DIR_ENV = "PI_CODING_AGENT_SESSION_DIR"
_NODE_BIN_ENV = "MERIDIAN_PI_NODE_BIN"
_INSTALLED_PI_BINARY_NAME = "pi"
_PACKAGED_BINARY_NAME = "meridian-pi"
_REQUIRED_AGENT_SUBDIRECTORIES: tuple[str, ...] = ("sessions", "extensions", "bin")
_SESSION_DIR_FLAG = "--session-dir"
_WRAPPER_RUNTIME_SCHEMA_VERSION = 1
_WRAPPER_RUNTIME_SELECTED_EVENT_TYPE = "meridian.pi.runtime.selected"
_WRAPPER_RUNTIME_ERROR_EVENT_TYPE = "meridian.pi.runtime.error"
_PRIMARY_SESSION_ROLE = "primary"
_REQUIRED_HELP_SURFACE_TOKEN_GROUPS: tuple[tuple[str, ...], ...] = (
    ("--mode",),
    ("rpc",),
    ("--session-dir",),
    ("--no-extensions",),
    ("-e", "--extension"),
)


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


def _resolve_agent_dir(cli_agent_dir: str | None, env: Mapping[str, str]) -> Path | None:
    """Resolve optional ``PI_CODING_AGENT_DIR`` override using CLI > env precedence."""

    if cli_agent_dir is not None:
        return Path(cli_agent_dir).expanduser()

    env_agent_dir = env.get(_WRAPPER_AGENT_DIR_ENV, "").strip()
    if env_agent_dir:
        return Path(env_agent_dir).expanduser()

    return None


def _resolve_session_dir(env: Mapping[str, str]) -> Path:
    """Resolve the Meridian-managed Pi session root."""

    session_dir_override = env.get(_PI_SESSION_DIR_ENV, "").strip()
    if session_dir_override:
        return Path(session_dir_override).expanduser()
    return get_user_home() / "meridian-pi" / "sessions"


def _ensure_agent_dir_layout(agent_dir: Path) -> None:
    """Create base Pi agent directory and required subdirectories."""

    agent_dir.mkdir(parents=True, exist_ok=True)
    for subdirectory in _REQUIRED_AGENT_SUBDIRECTORIES:
        (agent_dir / subdirectory).mkdir(parents=True, exist_ok=True)


def _ensure_session_dir(session_dir: Path) -> None:
    """Create managed Pi session storage root."""

    session_dir.mkdir(parents=True, exist_ok=True)


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

    base_name = _PACKAGED_BINARY_NAME
    windows_name = f"{_PACKAGED_BINARY_NAME}.exe"
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


def _build_child_env(
    base_env: Mapping[str, str],
    *,
    agent_dir: Path | None,
    session_dir: Path,
) -> dict[str, str]:
    """Build child environment with optional config-root and managed session root."""

    child_env = dict(base_env)
    if agent_dir is not None:
        child_env[_PI_AGENT_DIR_ENV] = str(agent_dir)
    child_env[_PI_SESSION_DIR_ENV] = str(session_dir)
    return child_env


def _node_fallback_available(runner_path: Path) -> bool:
    """Return whether source/dev Node fallback dependencies are available."""

    runtime_dependency_dir = (
        runner_path.parent / "node_modules" / "@earendil-works" / "pi-coding-agent"
    )
    return runner_path.is_file() and runtime_dependency_dir.is_dir()


def _print_error(message: str) -> None:
    print(f"meridian-pi: {message}", file=sys.stderr)


def _emit_wrapper_event(payload: Mapping[str, object]) -> None:
    try:
        print(json.dumps(payload, separators=(",", ":")), flush=True)
    except OSError:
        logger.debug("Failed to emit meridian-pi wrapper event", exc_info=True)


def _emit_runtime_error_event(
    *,
    error: str,
    phase: Literal["preflight", "exec"],
    runtime_kind: str | None = None,
    runtime_path: str | None = None,
) -> None:
    payload: dict[str, object] = {
        "type": _WRAPPER_RUNTIME_ERROR_EVENT_TYPE,
        "schema_version": _WRAPPER_RUNTIME_SCHEMA_VERSION,
        "error": error,
        "phase": phase,
    }
    if runtime_kind is not None:
        payload["runtime_kind"] = runtime_kind
    if runtime_path is not None:
        payload["runtime_path"] = runtime_path
    _emit_wrapper_event(payload)


def _env_truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _runtime_selected_payload(
    *,
    runtime_kind: str,
    command: Sequence[str],
    session_dir: Path,
    auth_policy: str,
    runtime_version: str,
) -> dict[str, object]:
    return {
        "type": _WRAPPER_RUNTIME_SELECTED_EVENT_TYPE,
        "schema_version": _WRAPPER_RUNTIME_SCHEMA_VERSION,
        "runtime_kind": runtime_kind,
        "runtime_path": command[0] if command else "",
        "runtime_version": runtime_version,
        "session_dir": str(session_dir),
        "auth_policy": auth_policy,
    }


def _persist_wrapper_runtime_metadata(
    env: Mapping[str, str],
    payload: Mapping[str, object],
) -> None:
    metadata_path_value = env.get(PI_WRAPPER_METADATA_PATH_ENV, "").strip()
    if not metadata_path_value:
        return

    metadata_path = Path(metadata_path_value).expanduser()
    try:
        atomic_write_text(
            metadata_path,
            json.dumps(payload, separators=(",", ":")) + "\n",
        )
    except OSError:
        logger.debug("Failed to persist meridian-pi wrapper runtime metadata", exc_info=True)


def _is_primary_session_role(env: Mapping[str, str]) -> bool:
    return env.get(_PI_SESSION_ROLE_ENV, "").strip().lower() == _PRIMARY_SESSION_ROLE


def _has_session_dir_flag(args: Sequence[str]) -> bool:
    for index, token in enumerate(args):
        if token == "--":
            break
        if token.startswith(f"{_SESSION_DIR_FLAG}="):
            return True
        if token != _SESSION_DIR_FLAG:
            continue
        if index + 1 < len(args):
            return True
    return False


def _inject_session_dir_flag(args: Sequence[str], session_dir: Path) -> list[str]:
    if _has_session_dir_flag(args):
        return list(args)
    return [_SESSION_DIR_FLAG, str(session_dir), *args]


def _resolve_installed_pi_binary(env: Mapping[str, str]) -> str | None:
    return shutil.which(_INSTALLED_PI_BINARY_NAME, path=env.get("PATH"))


def _runtime_version(command: Sequence[str], env: Mapping[str, str]) -> str | None:
    try:
        completed = subprocess.run(
            [command[0], "--version"],
            check=False,
            capture_output=True,
            text=True,
            env=dict(env),
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for candidate in (completed.stdout, completed.stderr):
        text = (candidate or "").strip()
        if text:
            return text.splitlines()[0]
    return None


def _probe_runtime_compatibility(binary_path: str, env: Mapping[str, str]) -> str | None:
    """Return ``None`` when runtime passes bounded version/help compatibility probes."""

    try:
        version_probe = subprocess.run(
            [binary_path, "--version"],
            check=False,
            capture_output=True,
            text=True,
            env=dict(env),
            timeout=2.0,
        )
    except FileNotFoundError:
        return "binary not found"
    except OSError as exc:
        return str(exc)
    except subprocess.SubprocessError as exc:
        return str(exc)

    if version_probe.returncode != 0:
        stderr = (version_probe.stderr or "").strip()
        stdout = (version_probe.stdout or "").strip()
        detail = stderr or stdout or f"exit {version_probe.returncode}"
        return f"`--version` probe failed: {detail}"

    try:
        help_probe = subprocess.run(
            [binary_path, "--help"],
            check=False,
            capture_output=True,
            text=True,
            env=dict(env),
            timeout=2.0,
        )
    except OSError as exc:
        return str(exc)
    except subprocess.SubprocessError as exc:
        return str(exc)

    if help_probe.returncode != 0:
        stderr = (help_probe.stderr or "").strip()
        stdout = (help_probe.stdout or "").strip()
        detail = stderr or stdout or f"exit {help_probe.returncode}"
        return f"`--help` probe failed: {detail}"

    help_surface = "\n".join(
        candidate for candidate in (help_probe.stdout, help_probe.stderr) if candidate
    )
    missing_groups = [
        "/".join(group)
        for group in _REQUIRED_HELP_SURFACE_TOKEN_GROUPS
        if not any(token in help_surface for token in group)
    ]
    if missing_groups:
        missing = ", ".join(missing_groups)
        return f"`--help` surface missing required flags: {missing}"

    return None


def _log_runtime_diagnostics(
    *,
    runtime_kind: str,
    command: Sequence[str],
    child_env: Mapping[str, str],
    agent_dir_policy: str,
    runtime_version: str,
) -> None:
    logger.warning(
        "meridian-pi runtime selected: kind=%s path=%s version=%s session_dir=%s auth_policy=%s",
        runtime_kind,
        command[0],
        runtime_version,
        child_env.get(_PI_SESSION_DIR_ENV, ""),
        agent_dir_policy,
    )


def _resolve_runtime_command(
    *,
    passthrough_args: Sequence[str],
    child_env: Mapping[str, str],
) -> tuple[list[str], Literal["override", "installed", "packaged", "node"]]:
    binary_override = child_env.get(_WRAPPER_BINARY_ENV, "").strip()
    if binary_override:
        return [str(Path(binary_override).expanduser()), *passthrough_args], "override"

    allow_bundled_fallback = _env_truthy(
        child_env.get(_WRAPPER_ALLOW_BUNDLED_FALLBACK_ENV, "")
    )
    bundled_auth_confirmed = _env_truthy(
        child_env.get(_WRAPPER_BUNDLED_AUTH_CONFIRMED_ENV, "")
    )

    rejected_installed_runtime: str | None = None
    installed_pi = _resolve_installed_pi_binary(child_env)
    if installed_pi:
        compatibility_error = _probe_runtime_compatibility(installed_pi, child_env)
        if compatibility_error is not None:
            rejected_installed_runtime = (
                "installed `pi` runtime failed compatibility probe "
                f"(`pi --version` + `pi --help`) at '{installed_pi}': {compatibility_error}"
            )
            logger.warning("meridian-pi runtime candidate rejected: %s", rejected_installed_runtime)
            if not allow_bundled_fallback:
                raise RuntimeError(
                    f"{rejected_installed_runtime}. "
                    f"Set {_WRAPPER_BINARY_ENV} to a compatible runtime, "
                    f"or set {_WRAPPER_ALLOW_BUNDLED_FALLBACK_ENV}=1 to try bundled/dev fallback."
                )
        else:
            return [installed_pi, *passthrough_args], "installed"

    if not allow_bundled_fallback:
        raise RuntimeError(
            "no compatible installed `pi` runtime found on PATH. "
            f"Install `{_INSTALLED_PI_BINARY_NAME}` or set {_WRAPPER_BINARY_ENV}, "
            f"or set {_WRAPPER_ALLOW_BUNDLED_FALLBACK_ENV}=1 to opt into bundled/dev fallback."
        )

    if not bundled_auth_confirmed:
        installed_context = (
            f" {rejected_installed_runtime}."
            if rejected_installed_runtime is not None
            else ""
        )
        raise RuntimeError(
            "bundled/dev fallback requires explicit auth/config confirmation and will not run by "
            "default."
            f"{installed_context} "
            f"Prefer installed `{_INSTALLED_PI_BINARY_NAME}` or set {_WRAPPER_BINARY_ENV}. "
            f"If you intentionally want bundled/dev fallback, set "
            f"{_WRAPPER_BUNDLED_AUTH_CONFIRMED_ENV}=1."
        )

    fallback_diagnostic = (
        f" installed candidate rejected earlier: {rejected_installed_runtime}."
        if rejected_installed_runtime is not None
        else ""
    )

    packaged_binary = _resolve_packaged_binary()
    if packaged_binary is not None:
        compatibility_error = _probe_runtime_compatibility(str(packaged_binary), child_env)
        if compatibility_error is not None:
            raise RuntimeError(
                "bundled `meridian-pi` runtime failed compatibility probe "
                "(`meridian-pi --version` + `meridian-pi --help`): "
                f"{compatibility_error}.{fallback_diagnostic}"
            )
        return [str(packaged_binary), *passthrough_args], "packaged"

    present_packaged_binary = _first_present_packaged_binary()
    if present_packaged_binary is not None:
        raise RuntimeError(
            "packaged Pi runtime binary is present but not executable on this host: "
            f"{present_packaged_binary}.{fallback_diagnostic}"
        )

    runner_path = _runner_path()
    if not _node_fallback_available(runner_path):
        raise RuntimeError(
            "bundled/dev fallback requested but runtime is unavailable; build it with "
            "scripts/build-meridian-pi-runtime.sh or set MERIDIAN_PI_BINARY; "
            f"source/dev fallback requires runtime deps installed.{fallback_diagnostic}"
        )

    node_bin = child_env.get(_NODE_BIN_ENV, "node").strip() or "node"
    return [node_bin, str(runner_path), *passthrough_args], "node"


def main() -> None:
    """Launch Pi with Meridian runtime/auth/session resolution policy."""

    emit_wrapper_events = not _is_primary_session_role(os.environ)

    try:
        passthrough_args, cli_agent_dir = _strip_agent_dir_flag(sys.argv[1:])
    except ValueError as error:
        if emit_wrapper_events:
            _emit_runtime_error_event(error=str(error), phase="preflight")
        _print_error(str(error))
        raise SystemExit(2) from error

    agent_dir = _resolve_agent_dir(cli_agent_dir, os.environ)
    session_dir = _resolve_session_dir(os.environ)

    try:
        _ensure_session_dir(session_dir)
        if agent_dir is not None:
            _ensure_agent_dir_layout(agent_dir)
    except OSError as error:
        if emit_wrapper_events:
            _emit_runtime_error_event(error=str(error), phase="preflight")
        if agent_dir is not None:
            _print_error(f"failed to create agent directory '{agent_dir}': {error}")
        else:
            _print_error(f"failed to create session directory '{session_dir}': {error}")
        raise SystemExit(1) from error

    final_passthrough_args = _inject_session_dir_flag(passthrough_args, session_dir)
    child_env = _build_child_env(os.environ, agent_dir=agent_dir, session_dir=session_dir)

    try:
        command, command_kind = _resolve_runtime_command(
            passthrough_args=final_passthrough_args,
            child_env=child_env,
        )
    except RuntimeError as error:
        if emit_wrapper_events:
            _emit_runtime_error_event(error=str(error), phase="preflight")
        _print_error(str(error))
        raise SystemExit(1) from error

    agent_dir_policy = (
        "explicit-agent-dir-override"
        if _PI_AGENT_DIR_ENV in child_env
        else "inherit-runtime-default-auth-config"
    )
    runtime_version = _runtime_version(command, child_env) or "unknown"
    runtime_selected_payload = _runtime_selected_payload(
        runtime_kind=command_kind,
        command=command,
        session_dir=session_dir,
        auth_policy=agent_dir_policy,
        runtime_version=runtime_version,
    )
    if emit_wrapper_events:
        _log_runtime_diagnostics(
            runtime_kind=command_kind,
            command=command,
            child_env=child_env,
            agent_dir_policy=agent_dir_policy,
            runtime_version=runtime_version,
        )
        _emit_wrapper_event(runtime_selected_payload)
    else:
        _persist_wrapper_runtime_metadata(child_env, runtime_selected_payload)

    try:
        completed = subprocess.run(command, env=child_env, check=False)
    except FileNotFoundError as error:
        runtime_error = (
            "Node.js runtime is required for meridian-pi (install Node.js 20+ and retry)."
            if command_kind == "node"
            else f"Pi runtime binary not found: {command[0]}"
        )
        if emit_wrapper_events:
            _emit_runtime_error_event(
                error=runtime_error,
                phase="exec",
                runtime_kind=command_kind,
                runtime_path=command[0],
            )
        if command_kind == "node":
            _print_error(runtime_error)
        else:
            _print_error(runtime_error)
        raise SystemExit(1) from error
    except OSError as error:
        runtime_error = (
            f"failed to execute Node.js runtime command '{command[0]}': {error}"
            if command_kind == "node"
            else f"failed to execute Pi runtime binary '{command[0]}': {error}"
        )
        if emit_wrapper_events:
            _emit_runtime_error_event(
                error=runtime_error,
                phase="exec",
                runtime_kind=command_kind,
                runtime_path=command[0],
            )
        if command_kind == "node":
            _print_error(runtime_error)
        else:
            _print_error(runtime_error)
        raise SystemExit(1) from error

    raise SystemExit(completed.returncode)


__all__ = [
    "_build_child_env",
    "_compiled_binary_candidate_names",
    "_compiled_binary_candidates",
    "_ensure_agent_dir_layout",
    "_ensure_session_dir",
    "_first_present_packaged_binary",
    "_has_session_dir_flag",
    "_inject_session_dir_flag",
    "_is_runnable_binary",
    "_node_fallback_available",
    "_probe_runtime_compatibility",
    "_resolve_agent_dir",
    "_resolve_installed_pi_binary",
    "_resolve_packaged_binary",
    "_resolve_runtime_command",
    "_resolve_session_dir",
    "_runner_path",
    "_strip_agent_dir_flag",
    "main",
]
