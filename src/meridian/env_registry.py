"""Registry for Meridian-owned environment variable contracts.

``MERIDIAN_*`` names are public contracts. ``_MERIDIAN_*`` names are
repo-internal transport. Add new names here before using them in Python or the
Pi runtime so their ownership and propagation rules stay explicit.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class EnvTier(StrEnum):
    """Name-stability tier for a Meridian environment variable."""

    PUBLIC = "public"
    INTERNAL = "internal"


class EnvSubtype(StrEnum):
    """Direction and use of a Meridian environment variable."""

    CONFIG_INPUT = "config-input"
    INJECTED_HANDLE = "injected-handle"
    HOOK_PAYLOAD = "hook-payload"
    INTER_TOOL = "inter-tool"


@dataclass(frozen=True)
class EnvVar:
    """One concrete Meridian environment variable contract."""

    name: str
    tier: EnvTier
    subtype: EnvSubtype
    purpose: str
    child: bool = False
    hook_field: str | None = None


@dataclass(frozen=True)
class EnvFamily:
    """A validated dynamic family whose middle component is data-derived."""

    prefix: str
    suffix: str
    tier: EnvTier
    subtype: EnvSubtype
    purpose: str
    child: bool = False

    def contains(self, name: str) -> bool:
        if not name.startswith(self.prefix) or not name.endswith(self.suffix):
            return False
        middle_end = len(name) - len(self.suffix) if self.suffix else len(name)
        middle = name[len(self.prefix) : middle_end]
        return bool(middle) and middle[0].isalpha() and all(
            char.isupper() or char.isdigit() or char == "_" for char in middle
        )


def _public_config(name: str, purpose: str) -> EnvVar:
    return EnvVar(name, EnvTier.PUBLIC, EnvSubtype.CONFIG_INPUT, purpose)


def _public_handle(name: str, purpose: str, *, hook_field: str | None = None) -> EnvVar:
    return EnvVar(
        name,
        EnvTier.PUBLIC,
        EnvSubtype.INJECTED_HANDLE,
        purpose,
        child=True,
        hook_field=hook_field,
    )


def _hook_payload(name: str, purpose: str, hook_field: str) -> EnvVar:
    return EnvVar(
        name,
        EnvTier.PUBLIC,
        EnvSubtype.HOOK_PAYLOAD,
        purpose,
        hook_field=hook_field,
    )


def _internal_handle(name: str, purpose: str, *, child: bool = True) -> EnvVar:
    return EnvVar(name, EnvTier.INTERNAL, EnvSubtype.INJECTED_HANDLE, purpose, child=child)


ENV_VARS: tuple[EnvVar, ...] = (
    # Public configuration inputs.
    _public_config("MERIDIAN_AGENT", "Default agent selection."),
    _public_config("MERIDIAN_APPROVAL", "Harness approval policy override."),
    _public_config("MERIDIAN_AUTOCOMPACT", "Automatic context compaction toggle."),
    _public_config("MERIDIAN_AUTOCOMPACT_PCT", "Automatic compaction threshold."),
    _public_config("MERIDIAN_CONFIG", "Explicit Meridian configuration file."),
    _public_config("MERIDIAN_DEBUG", "Debug telemetry output toggle."),
    _public_config("MERIDIAN_DEFAULT_WAIT_YIELD_SECONDS", "Default wait yield interval."),
    _public_config("MERIDIAN_EFFORT", "Harness reasoning effort override."),
    _public_config("MERIDIAN_FORMAT", "CLI output format override."),
    _public_config("MERIDIAN_GUARDRAIL_TIMEOUT_MINUTES", "Guardrail execution timeout."),
    _public_config("MERIDIAN_HARNESS_MODEL_CLAUDE", "Claude harness model mapping."),
    _public_config("MERIDIAN_HARNESS_MODEL_CODEX", "Codex harness model mapping."),
    _public_config("MERIDIAN_HARNESS_MODEL_OPENCODE", "OpenCode harness model mapping."),
    _public_config(
        "MERIDIAN_HARNESS_WAIT_YIELD_SECONDS_CLAUDE", "Claude wait yield interval."
    ),
    _public_config(
        "MERIDIAN_HARNESS_WAIT_YIELD_SECONDS_CODEX", "Codex wait yield interval."
    ),
    _public_config(
        "MERIDIAN_HARNESS_WAIT_YIELD_SECONDS_OPENCODE", "OpenCode wait yield interval."
    ),
    _public_config("MERIDIAN_HOME", "Meridian user-state root override."),
    _public_config("MERIDIAN_HOOKS_ENABLED", "Hook dispatch toggle."),
    _public_config("MERIDIAN_KILL_GRACE_MINUTES", "Child termination grace period."),
    _public_config("MERIDIAN_MAX_DEPTH", "Maximum delegated spawn depth."),
    _public_config("MERIDIAN_MAX_RETRIES", "Maximum spawn retry count."),
    _public_config("MERIDIAN_MIN_WAIT_YIELD_SECONDS", "Minimum wait yield interval."),
    _public_config("MERIDIAN_MODEL", "Default model selection."),
    _public_config("MERIDIAN_PI_BINARY", "Pi executable override."),
    _public_config(
        "MERIDIAN_PI_CHILD_WAVE_TIMEOUT_SECONDS", "Pi child-wave timeout configuration."
    ),
    _public_config("MERIDIAN_PI_DISABLE_MANAGED_BASH", "Disable Pi managed-bash extension."),
    _public_config(
        "MERIDIAN_PI_EXTENSION_INSTALL_ROOT", "Installed Pi extension root override."
    ),
    _public_config(
        "MERIDIAN_PI_EXTENSION_SOURCE_ROOT", "Pi extension source root override."
    ),
    _public_config("MERIDIAN_PI_LOAD_ALL_EXTENSIONS", "Load all Pi extension bundles."),
    _public_config("MERIDIAN_PI_MANAGED_BASH", "Pi managed-bash compatibility setting."),
    _public_config(
        "MERIDIAN_PI_TASK_PING_INTERVAL_SECONDS", "Pi managed-task ping interval."
    ),
    _public_config("MERIDIAN_RESIDENT_DEADLINE_SECONDS", "Resident spawn deadline."),
    _public_config("MERIDIAN_RESIDENT_POLL_SECONDS", "Resident spawn poll interval."),
    _public_config("MERIDIAN_RESIDENT_REARM_BUDGET", "Resident spawn rearm budget."),
    _public_config("MERIDIAN_RETRY_BACKOFF_SECONDS", "Spawn retry backoff."),
    _public_config("MERIDIAN_SANDBOX", "Harness sandbox policy override."),
    _public_config("MERIDIAN_STARTUP_TIMEOUT_MINUTES", "Harness startup timeout."),
    _public_config("MERIDIAN_STATE_RETENTION_DAYS", "Runtime-state retention period."),
    _public_config("MERIDIAN_TIMEOUT", "Spawn timeout override."),
    _public_config("MERIDIAN_WAIT_TIMEOUT_MINUTES", "Wait operation timeout."),
    # Public handles injected for agents, prompts, and tools.
    _public_handle(
        "MERIDIAN_ACTIVE_WORK_DIR", "Active work artifact directory.", hook_field="work_dir"
    ),
    _public_handle("MERIDIAN_ACTIVE_WORK_ID", "Active work identifier.", hook_field="work_id"),
    _public_handle("MERIDIAN_CHAT_ID", "Current Meridian chat identifier."),
    _public_handle(
        "MERIDIAN_PROJECT_DIR",
        "Current Meridian project directory.",
        hook_field="project_root",
    ),
    _public_handle("MERIDIAN_SPAWN_ID", "Current spawn identifier.", hook_field="spawn_id"),
    _public_handle("MERIDIAN_TASK_DIR", "Logical task checkout directory."),
    # Public hook payload fields.
    _hook_payload("MERIDIAN_HOOK_EVENT", "Hook event name.", "event_name"),
    _hook_payload("MERIDIAN_HOOK_EVENT_ID", "Hook event identifier.", "event_id"),
    _hook_payload("MERIDIAN_HOOK_SCHEMA_VERSION", "Hook payload schema version.", "schema_version"),
    _hook_payload("MERIDIAN_HOOK_TIMESTAMP", "Hook event timestamp.", "timestamp"),
    _hook_payload("MERIDIAN_SPAWN_AGENT", "Hook spawn agent.", "spawn_agent"),
    _hook_payload("MERIDIAN_SPAWN_COST_USD", "Hook spawn cost.", "spawn_cost_usd"),
    _hook_payload("MERIDIAN_SPAWN_DURATION_SECS", "Hook spawn duration.", "spawn_duration_secs"),
    _hook_payload("MERIDIAN_SPAWN_ERROR", "Hook spawn error.", "spawn_error"),
    _hook_payload("MERIDIAN_SPAWN_MODEL", "Hook spawn model.", "spawn_model"),
    _hook_payload("MERIDIAN_SPAWN_STATUS", "Hook spawn status.", "spawn_status"),
    # Public inter-tool signal consumed by mars-agents.
    EnvVar(
        "MERIDIAN_MANAGED",
        EnvTier.PUBLIC,
        EnvSubtype.INTER_TOOL,
        "Marks invocation through Meridian for mars-agents.",
    ),
    # Internal handles use their pre-migration names until the flag-day rename.
    _internal_handle("MERIDIAN_DEPTH", "Current delegated spawn depth."),
    _internal_handle("MERIDIAN_PARENT_SPAWN_ID", "Parent spawn identifier."),
    _internal_handle("MERIDIAN_RUNTIME_DIR", "Resolved per-project runtime directory."),
    _internal_handle("MERIDIAN_HARNESS", "Harness identity propagated one level."),
    _internal_handle("MERIDIAN_PROJECT_ROOT", "Legacy child control-root projection."),
    _internal_handle("MERIDIAN_TASK_CWD", "Legacy physical child working directory."),
    _internal_handle("MERIDIAN_GUARDRAIL_RUN_ID", "Guardrail run identifier."),
    _internal_handle("MERIDIAN_GUARDRAIL_OUTPUT_LOG", "Guardrail output log path."),
    _internal_handle("MERIDIAN_GUARDRAIL_REPORT_PATH", "Guardrail report path."),
    _internal_handle(
        "MERIDIAN_PRIMARY_STDERR_LOG_PATH", "Primary subprocess stderr path.", child=False
    ),
    _internal_handle("MERIDIAN_PI_STATE_DIR", "Pi extension runtime-state root."),
    _internal_handle("MERIDIAN_PI_SESSION_ROLE", "Pi primary or spawned session role."),
    _internal_handle("MERIDIAN_PI_BASH_ID", "Originating managed-bash task identifier."),
    _internal_handle("MERIDIAN_PI_CHILD_WAVE_TIMEOUT_MS", "Resolved Pi child-wave timeout."),
    _internal_handle("MERIDIAN_PI_TASK_PING_INTERVAL_MS", "Resolved Pi task-ping interval."),
    _internal_handle(
        "MERIDIAN_PI_TASK_PING_RESET_ON_ACTIVITY", "Resolved Pi task-ping reset policy."
    ),
    _internal_handle(
        "MERIDIAN_PI_BACKGROUND_TASKS_ENABLED", "Resolved Pi background-task bundle toggle."
    ),
    _internal_handle(
        "MERIDIAN_PI_SPAWN_WATCH_ENABLED", "Resolved Pi spawn-watch bundle toggle."
    ),
)

ENV_FAMILIES: tuple[EnvFamily, ...] = (
    EnvFamily(
        prefix="MERIDIAN_CONTEXT_",
        suffix="_DIR",
        tier=EnvTier.PUBLIC,
        subtype=EnvSubtype.INJECTED_HANDLE,
        purpose="Named context directory exposed to agents and prompts.",
        child=True,
    ),
    EnvFamily(
        prefix="MERIDIAN_SECRET_",
        suffix="",
        tier=EnvTier.PUBLIC,
        subtype=EnvSubtype.CONFIG_INPUT,
        purpose="Explicit secret input removed at child boundaries by default.",
    ),
)

ENV_VAR_BY_NAME: dict[str, EnvVar] = {item.name: item for item in ENV_VARS}
if len(ENV_VAR_BY_NAME) != len(ENV_VARS):  # pragma: no cover - import-time invariant
    raise RuntimeError("Duplicate Meridian environment variable registry entry")

ALLOWED_CHILD_ENV_KEYS: frozenset[str] = frozenset(
    item.name for item in ENV_VARS if item.child
)
HOOK_PAYLOAD_ENV_KEYS: dict[str, str] = {
    item.hook_field: item.name for item in ENV_VARS if item.hook_field is not None
}


def is_registered_env_name(name: str) -> bool:
    """Return whether *name* is an exact entry or a valid dynamic family member."""

    return name in ENV_VAR_BY_NAME or any(family.contains(name) for family in ENV_FAMILIES)


def is_allowed_child_env_name(name: str) -> bool:
    """Return whether the registry permits bind-time child injection of *name*."""

    if name in ALLOWED_CHILD_ENV_KEYS:
        return True
    return any(family.child and family.contains(name) for family in ENV_FAMILIES)


__all__ = [
    "ALLOWED_CHILD_ENV_KEYS",
    "ENV_FAMILIES",
    "ENV_VARS",
    "ENV_VAR_BY_NAME",
    "HOOK_PAYLOAD_ENV_KEYS",
    "EnvFamily",
    "EnvSubtype",
    "EnvTier",
    "EnvVar",
    "is_allowed_child_env_name",
    "is_registered_env_name",
]

