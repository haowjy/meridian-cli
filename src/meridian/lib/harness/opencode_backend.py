"""OpenCode backend version seam.

OpenCode 1.x and 2.x are distinct runtimes: different server APIs, event names,
and on-disk storage schemas. This module resolves which major version a spawn
talks to. V1 is frozen; V2 is the target.

Nothing here starts a server or writes state. It probes ``opencode --version``
at most once per resolution, and launch bind resolves once per launch: the spec carries
the result, which every reader takes through ``bound_opencode_version``. Concrete transports live in
``connections/opencode_http.py`` (V1) and ``connections/opencode_v2_http.py``
(V2); ``OpenCodeConnection`` selects one from the resolved version. Version
capabilities are not modeled here — the adapter's ``HarnessCapabilities`` is the
consumer contract, and per-version capability descriptors were dead scaffolding.
"""

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
#: The launch env carries the ``[harness.opencode] version`` preference under this name.
OPENCODE_VERSION_ENV = "MERIDIAN_HARNESS_OPENCODE_VERSION"
VALID_VERSION_VALUES: tuple[str, ...] = VERSION_PREFERENCES

#: Version assumed when the installed binary cannot be probed. V2 is the current
#: GA runtime; an unprobeable binary fails the spawn later regardless.
DEFAULT_OPENCODE_VERSION: OpenCodeVersion = "v2"

_VERSION_RE = re.compile(r"(?P<major>\d+)\.(?P<minor>\d+)")
_V2_MAJOR = 2


def parse_opencode_version(output: str) -> OpenCodeVersion | None:
    """Map ``opencode --version`` output to a backend version.

    V1 prints a bare semver (``1.18.31``); V2 prefixes it (``opencode v2.0.6``).
    Returns ``None`` when no version number is present.
    """
    match = _VERSION_RE.search(output or "")
    if match is None:
        return None
    return "v2" if int(match.group("major")) >= _V2_MAJOR else "v1"


def detect_opencode_version(
    binary: str = "opencode", *, env: Mapping[str, str], timeout_seconds: float = 10.0
) -> OpenCodeVersion | None:
    """Probe ``binary --version``. Returns ``None`` when it cannot be run."""
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
    """Coerce a config value to a preferred backend version, defaulting to auto."""
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
    """Resolve the backend version from config preference plus binary probe.

    An explicit ``v1``/``v2`` preference always wins. ``auto`` probes the binary
    and falls back to :data:`DEFAULT_OPENCODE_VERSION` when the probe fails.
    """
    normalized = normalize_version_preference(preference)
    if normalized in ("v1", "v2"):
        return normalized
    resolved = detected if detected is not None else detect_opencode_version(binary, env=env)
    return resolved or DEFAULT_OPENCODE_VERSION


def v1_session_running(payload: object, session_id: str) -> bool:
    """Whether ``GET /session/status`` reports *session_id* as executing.

    The listing maps every session the server knows to its ``SessionStatus``.
    ``busy`` is a turn in progress and ``retry`` is a provider backoff that turn
    is waiting out; a session the map omits is idle. Asked by the connection on a
    liveness stall, so a long tool is not mistaken for a dead stream.
    """

    statuses = json_object(payload)
    if statuses is None:
        raise ValueError("session status listing is not an object")
    entry = statuses.get(session_id)
    if entry is None:
        return False
    status = json_object(entry)
    if status is None:
        raise ValueError("session status entry is not an object")
    return status.get("type") in ("busy", "retry")


def v2_session_running(payload: object, session_id: str) -> bool:
    """Whether ``GET /api/session/active`` lists *session_id* as executing.

    Shared by the idle sensor (after a reconnect) and the connection (on a
    liveness stall), so both read the server's answer the same way.
    """

    response = json_object(payload)
    active = json_object(response.get("data")) if response is not None else None
    if active is None:
        raise ValueError("active sessions listing has no data object")
    return session_id in active


def bound_opencode_version(version: OpenCodeVersion | None) -> OpenCodeVersion:
    """The version launch bind resolved onto the spec (``spec.opencode_version``).

    The permission compile, the config projection, the managed-primary preview and the
    connection all read it, so the report and the launch cannot disagree. A spec that did
    not pass through bind has none, and nothing may guess one for it.
    """

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
