from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping
from typing import Literal

import structlog

from meridian.lib.utils.json_shapes import json_object

logger = structlog.get_logger(__name__)

OpenCodeVersion = Literal["v1", "v2"]
OpenCodeVersionPreference = Literal["auto", "v1", "v2"]

VERSION_PREFERENCES: tuple[OpenCodeVersionPreference, ...] = ("auto", "v1", "v2")
OPENCODE_VERSION_ENV = "MERIDIAN_HARNESS_OPENCODE_VERSION"
VALID_VERSION_VALUES: tuple[str, ...] = VERSION_PREFERENCES

DEFAULT_OPENCODE_VERSION: OpenCodeVersion = "v2"

_VERSION_RE = re.compile(r"(?P<major>\d+)\.(?P<minor>\d+)")
_V2_MAJOR = 2


def parse_opencode_version(output: str) -> OpenCodeVersion | None:
    match = _VERSION_RE.search(output or "")
    if match is None:
        return None
    return "v2" if int(match.group("major")) >= _V2_MAJOR else "v1"


def detect_opencode_version(
    binary: str = "opencode", *, env: Mapping[str, str], timeout_seconds: float = 10.0
) -> OpenCodeVersion | None:
    try:
        completed = subprocess.run(
            [binary, "--version"],
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("Could not probe OpenCode version", binary=binary, error=str(exc))
        return None
    if completed.returncode != 0:
        logger.debug(
            "OpenCode version probe failed",
            binary=binary,
            returncode=completed.returncode,
        )
        return None
    return parse_opencode_version(completed.stdout or completed.stderr)


def normalize_version_preference(value: str | None) -> OpenCodeVersionPreference:
    normalized = (value or "").strip().lower()
    if normalized in VERSION_PREFERENCES:
        return normalized
    return "auto"


def resolve_opencode_version(
    preference: str | None = "auto",
    *,
    env: Mapping[str, str],
    binary: str = "opencode",
    detected: OpenCodeVersion | None = None,
) -> OpenCodeVersion:
    normalized = normalize_version_preference(preference)
    if normalized in ("v1", "v2"):
        return normalized
    resolved = detected if detected is not None else detect_opencode_version(binary, env=env)
    return resolved or DEFAULT_OPENCODE_VERSION


def v1_session_running(payload: object, session_id: str) -> bool:
    statuses = json_object(payload)
    if statuses is None:
        raise ValueError("session status listing is not an object")
    entry = statuses.get(session_id)
    if entry is None:
        return False
    status = json_object(entry)
    if status is None:
        raise ValueError("session status entry is not an object")
    status_type = status.get("type")
    if not isinstance(status_type, str):
        raise ValueError("session status entry has no string type")
    if status_type == "idle":
        return False
    if status_type in ("busy", "retry"):
        return True
    # An unknown status might be active; treating it as idle would end a live stream.
    raise ValueError(f"unknown session status type: {status_type}")


def v2_session_running(payload: object, session_id: str) -> bool:
    response = json_object(payload)
    active = json_object(response.get("data")) if response is not None else None
    if active is None:
        raise ValueError("active sessions listing has no data object")
    return session_id in active


def bound_opencode_version(version: OpenCodeVersion | None) -> OpenCodeVersion:
    if version is None:
        raise ValueError("OpenCode launch spec has no backend version; bind resolves it")
    return version


__all__ = [
    "DEFAULT_OPENCODE_VERSION",
    "OPENCODE_VERSION_ENV",
    "VALID_VERSION_VALUES",
    "VERSION_PREFERENCES",
    "OpenCodeVersion",
    "OpenCodeVersionPreference",
    "bound_opencode_version",
    "detect_opencode_version",
    "normalize_version_preference",
    "parse_opencode_version",
    "resolve_opencode_version",
    "v1_session_running",
    "v2_session_running",
]
