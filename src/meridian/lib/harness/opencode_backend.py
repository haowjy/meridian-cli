"""OpenCode backend seam.

OpenCode 1.x and 2.x are distinct runtimes: different server APIs, event names,
and on-disk storage schemas. This module owns the decision of which backend a
spawn talks to and what each version can do. V1 is frozen; V2 is the target.

Nothing here starts a server or writes state. It probes ``opencode --version``
at most once per resolution and exposes a capability descriptor. The concrete
transports live behind the :class:`OpenCodeBackend` protocol and are selected
from the resolved version.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Literal, Protocol, cast, runtime_checkable

import structlog

logger = structlog.get_logger(__name__)

OpenCodeVersion = Literal["v1", "v2"]
OpenCodeVersionPreference = Literal["auto", "v1", "v2"]
OpenCodeStorageLayout = Literal["sqlite_v1", "sqlite_v2"]

VERSION_PREFERENCES: tuple[OpenCodeVersionPreference, ...] = ("auto", "v1", "v2")
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
    binary: str = "opencode", *, timeout_seconds: float = 10.0
) -> OpenCodeVersion | None:
    """Probe ``binary --version``. Returns ``None`` when it cannot be run."""
    try:
        completed = subprocess.run(
            [binary, "--version"],
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
        return cast("OpenCodeVersionPreference", normalized)
    return "auto"


def resolve_opencode_version(
    preference: str | None = "auto",
    *,
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
    resolved = detected if detected is not None else detect_opencode_version(binary)
    return resolved or DEFAULT_OPENCODE_VERSION


@dataclass(frozen=True, slots=True)
class OpenCodeCapabilities:
    """What one OpenCode backend can do; consumed by adapter and launch policy."""

    version: OpenCodeVersion
    uses_server_api: bool
    storage_layout: OpenCodeStorageLayout
    supports_named_primary_resume: bool
    supports_model_switch_on_resume: bool
    terminal_event_types: tuple[str, ...]

    @property
    def is_v2(self) -> bool:
        return self.version == "v2"


_V1_CAPABILITIES = OpenCodeCapabilities(
    version="v1",
    uses_server_api=True,
    storage_layout="sqlite_v1",
    supports_named_primary_resume=False,
    supports_model_switch_on_resume=False,
    terminal_event_types=("session.idle", "session.error"),
)

_V2_CAPABILITIES = OpenCodeCapabilities(
    version="v2",
    uses_server_api=True,
    storage_layout="sqlite_v2",
    supports_named_primary_resume=True,
    supports_model_switch_on_resume=True,
    terminal_event_types=(
        "session.execution.succeeded",
        "session.execution.failed",
        "session.execution.interrupted",
    ),
)

_CAPABILITIES_BY_VERSION: dict[OpenCodeVersion, OpenCodeCapabilities] = {
    "v1": _V1_CAPABILITIES,
    "v2": _V2_CAPABILITIES,
}


def capabilities_for_version(version: OpenCodeVersion) -> OpenCodeCapabilities:
    return _CAPABILITIES_BY_VERSION[version]


@runtime_checkable
class OpenCodeBackend(Protocol):
    """Runtime seam between Meridian and one OpenCode major version.

    V1 wraps the frozen ``opencode serve`` HTTP+SSE path. V2 targets the session
    API and ``/api/event`` stream. Implementations are registered per version.
    """

    @property
    def version(self) -> OpenCodeVersion: ...

    @property
    def capabilities(self) -> OpenCodeCapabilities: ...


__all__ = [
    "DEFAULT_OPENCODE_VERSION",
    "VALID_VERSION_VALUES",
    "VERSION_PREFERENCES",
    "OpenCodeBackend",
    "OpenCodeCapabilities",
    "OpenCodeStorageLayout",
    "OpenCodeVersion",
    "OpenCodeVersionPreference",
    "capabilities_for_version",
    "detect_opencode_version",
    "normalize_version_preference",
    "parse_opencode_version",
    "resolve_opencode_version",
]
