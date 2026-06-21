"""Startup policy enums for command classification."""

from enum import StrEnum


class StartupClass(StrEnum):
    """High-level startup tier for a CLI invocation."""

    TRIVIAL = "trivial"
    READ_ROOTLESS = "read_rootless"
    READ_PROJECT = "read_project"
    READ_RUNTIME = "read_runtime"
    WRITE_PROJECT = "write_project"
    WRITE_RUNTIME = "write_runtime"
    PRIMARY_LAUNCH = "primary_launch"
    SERVICE_ROOTLESS = "service_rootless"
    SERVICE_RUNTIME = "service_runtime"


class StateRequirement(StrEnum):
    """Filesystem state preparation required before command execution."""

    NONE = "none"
    PROJECT_READ = "project_read"
    RUNTIME_READ = "runtime_read"
    PROJECT_WRITE = "project_write"
    RUNTIME_WRITE = "runtime_write"


class TelemetryMode(StrEnum):
    """Telemetry sink mode selected by startup policy."""

    NONE = "none"
    STDERR = "stderr"
    SEGMENT = "segment"
    SEGMENT_OPTIONAL = "segment_optional"


class RootSource(StrEnum):
    """Source used to resolve the target project root."""

    CWD = "cwd"
    ARGV = "argv"
