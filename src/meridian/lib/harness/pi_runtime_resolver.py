"""Pi runtime resolution and compatibility probes for harness launches."""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

PiLaunchRole = Literal["primary", "spawned"]

_PI_BINARY_ENV: Final[str] = "MERIDIAN_PI_BINARY"
_PI_BINARY_NAME: Final[str] = "pi"

_REQUIRED_HELP_SURFACE_TOKEN_GROUPS_PRIMARY: Final[tuple[tuple[str, ...], ...]] = (
    ("--model",),
    ("--append-system-prompt",),
    ("--session",),
    ("--fork",),
    ("--session-dir", "PI_CODING_AGENT_SESSION_DIR"),
)
_REQUIRED_HELP_SURFACE_TOKEN_GROUPS_SPAWNED: Final[tuple[tuple[str, ...], ...]] = (
    ("--mode",),
    ("rpc",),
    ("--model",),
    ("--append-system-prompt",),
    ("--session",),
    ("--fork",),
    ("--session-dir", "PI_CODING_AGENT_SESSION_DIR"),
    ("--no-extensions",),
    ("--no-skills",),
    ("--no-context-files",),
    ("--no-prompt-templates",),
    ("-e", "--extension"),
)

_MISSING_RUNTIME_MESSAGE: Final[str] = (
    "Pi is not installed or not on PATH.\n"
    "Install Pi using the official Pi instructions, then run `pi --version` and retry.\n"
    "Set MERIDIAN_PI_BINARY=/path/to/pi to use a non-PATH installation."
)


@dataclass(frozen=True)
class PiRuntimeResolution:
    """Resolved Pi runtime details for one launch."""

    binary_path: str
    runtime_kind: Literal["override", "path"]
    runtime_version: str


class PiRuntimeResolutionError(RuntimeError):
    """Raised when no compatible installed Pi runtime can be resolved."""


@dataclass(frozen=True)
class _ProbeFailure:
    kind: Literal["execution", "compatibility"]
    detail: str


def resolve_pi_runtime(*, env: Mapping[str, str], role: PiLaunchRole) -> PiRuntimeResolution:
    """Resolve one compatible Pi runtime binary for a launch role."""

    override = env.get(_PI_BINARY_ENV, "").strip()
    if override:
        binary_path = str(Path(override).expanduser())
        runtime_kind: Literal["override", "path"] = "override"
    else:
        detected = shutil.which(_PI_BINARY_NAME, path=env.get("PATH"))
        if detected is None:
            raise PiRuntimeResolutionError(_MISSING_RUNTIME_MESSAGE)
        binary_path = detected
        runtime_kind = "path"

    compatibility_error = _probe_runtime_compatibility(binary_path=binary_path, env=env, role=role)
    if compatibility_error is not None:
        if compatibility_error.kind == "execution":
            raise PiRuntimeResolutionError(
                f"Unable to execute Pi at {binary_path}: {compatibility_error.detail}.\n"
                "Verify the binary path and permissions, run `pi --version`, or set "
                "MERIDIAN_PI_BINARY=/path/to/pi to another Pi binary."
            )
        raise PiRuntimeResolutionError(
            "Installed Pi at "
            f"{binary_path} is not compatible with Meridian's Pi harness: "
            f"{compatibility_error.detail}.\n"
            "Run `pi update`, or set MERIDIAN_PI_BINARY=/path/to/pi to another compatible "
            "Pi binary."
        )

    return PiRuntimeResolution(
        binary_path=binary_path,
        runtime_kind=runtime_kind,
        runtime_version=_runtime_version(binary_path=binary_path, env=env) or "unknown",
    )


def _probe_runtime_compatibility(
    *,
    binary_path: str,
    env: Mapping[str, str],
    role: PiLaunchRole,
) -> _ProbeFailure | None:
    version_probe = _run_probe_command((binary_path, "--version"), env)
    if isinstance(version_probe, _ProbeFailure):
        return version_probe
    if version_probe.returncode != 0:
        return _ProbeFailure(
            kind="execution",
            detail=_probe_failure_detail("--version", version_probe),
        )

    help_probe = _run_probe_command((binary_path, "--help"), env)
    if isinstance(help_probe, _ProbeFailure):
        return help_probe
    if help_probe.returncode != 0:
        return _ProbeFailure(
            kind="execution",
            detail=_probe_failure_detail("--help", help_probe),
        )

    required_groups = (
        _REQUIRED_HELP_SURFACE_TOKEN_GROUPS_SPAWNED
        if role == "spawned"
        else _REQUIRED_HELP_SURFACE_TOKEN_GROUPS_PRIMARY
    )
    if not required_groups:
        return None

    help_surface = "\n".join(
        candidate for candidate in (help_probe.stdout, help_probe.stderr) if candidate
    )
    missing_groups = [
        "/".join(group)
        for group in required_groups
        if not any(_help_surface_contains_token(help_surface, token) for token in group)
    ]
    if missing_groups:
        missing = ", ".join(missing_groups)
        return _ProbeFailure(
            kind="compatibility",
            detail=f"`--help` surface missing required flags: {missing}",
        )

    return None


def _runtime_version(*, binary_path: str, env: Mapping[str, str]) -> str | None:
    completed = _run_probe_command((binary_path, "--version"), env)
    if isinstance(completed, _ProbeFailure):
        return None
    for candidate in (completed.stdout, completed.stderr):
        text = (candidate or "").strip()
        if text:
            return text.splitlines()[0]
    return None


def _run_probe_command(
    command: Sequence[str],
    env: Mapping[str, str],
) -> subprocess.CompletedProcess[str] | _ProbeFailure:
    try:
        return subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            env=dict(env),
            timeout=2.0,
        )
    except FileNotFoundError:
        return _ProbeFailure(kind="execution", detail="binary not found")
    except OSError as exc:
        return _ProbeFailure(kind="execution", detail=str(exc))
    except subprocess.SubprocessError as exc:
        return _ProbeFailure(kind="execution", detail=str(exc))


def _probe_failure_detail(flag: str, probe: subprocess.CompletedProcess[str]) -> str:
    stderr = (probe.stderr or "").strip()
    stdout = (probe.stdout or "").strip()
    detail = stderr or stdout or f"exit {probe.returncode}"
    return f"`{flag}` probe failed: {detail}"


def _help_surface_contains_token(help_surface: str, token: str) -> bool:
    if token.startswith("-"):
        pattern = re.compile(rf"(?<!\S){re.escape(token)}(?:[=,\s]|$)")
        return pattern.search(help_surface) is not None
    pattern = re.compile(rf"(?<![A-Za-z0-9_/-]){re.escape(token)}(?![A-Za-z0-9_/-])")
    return pattern.search(help_surface) is not None


__all__ = [
    "PiLaunchRole",
    "PiRuntimeResolution",
    "PiRuntimeResolutionError",
    "resolve_pi_runtime",
]
