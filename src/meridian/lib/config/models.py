"""Typed models for Meridian operational configuration."""

from typing import Annotated

from pydantic import (
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from meridian.lib.config.catalog import file_alias
from meridian.lib.config.harness_models import HarnessConfig, HarnessProfileConfig
from meridian.lib.config.history_models import HistoryConfig
from meridian.lib.config.model_base import (
    ConfigModel,
    normalize_required_string,
    normalize_string_sequence,
)
from meridian.lib.config.schema import ConfigOptionMeta, ValueKind, config_field
from meridian.lib.core.domain import SkillDeliveryMode
from meridian.lib.core.overrides import (
    ApprovalValue,
    AutocompactPctValue,
    AutocompactValue,
    EffortValue,
)
from meridian.lib.utils.json_shapes import is_json_object

_OUTPUT_VERBOSITY_PRESETS = frozenset({"quiet", "normal", "verbose", "debug"})
_HARNESS_TABLE_KEYS = frozenset({"claude", "codex", "opencode", "pi"})
ADHOC_STALE_RETENTION_DAYS = 7


class OutputConfig(ConfigModel):
    """Terminal output filtering configuration for run streaming."""

    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    show: Annotated[
        tuple[str, ...],
        config_field(
            "output.show",
            value_kind="str_list",
            file_aliases=(file_alias("output", "show"),),
        ),
    ] = ("lifecycle", "sub-run", "error", "system")
    verbosity: Annotated[
        str | None,
        config_field(
            "output.verbosity",
            value_kind="verbosity",
            file_aliases=(file_alias("output", "verbosity"),),
        ),
    ] = None
    format: Annotated[
        str,
        config_field(
            "output.format",
            value_kind="str",
            file_aliases=(file_alias("output", "format"),),
            env_vars=("MERIDIAN_FORMAT",),
            command_visible=False,
        ),
    ] = "text"

    @field_validator("show", mode="before")
    @classmethod
    def _validate_show(cls, value: object) -> tuple[str, ...]:
        return normalize_string_sequence(value, source="output.show")

    @field_validator("verbosity")
    @classmethod
    def _validate_verbosity(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_required_string(value, source="output.verbosity").lower()
        if normalized not in _OUTPUT_VERBOSITY_PRESETS:
            raise ValueError(
                "Invalid value for 'output.verbosity': expected one of "
                f"{sorted(_OUTPUT_VERBOSITY_PRESETS)}, got {value!r}."
            )
        return normalized

    @field_validator("format")
    @classmethod
    def _validate_format(cls, value: str) -> str:
        return normalize_required_string(value, source="output.format")


class StateConfig(ConfigModel):
    """State retention settings for project and spawn artifacts."""

    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    retention_days: Annotated[
        int,
        config_field(
            "state.retention_days",
            value_kind="int",
            file_aliases=(
                file_alias("state", "retention_days"),
                file_alias(None, "retention_days"),
            ),
            env_vars=("MERIDIAN_STATE_RETENTION_DAYS",),
        ),
    ] = 30
    adhoc_retention_days: Annotated[
        int,
        config_field(
            "state.adhoc_retention_days",
            value_kind="int",
            file_aliases=(file_alias("state", "adhoc_retention_days"),),
            env_vars=("MERIDIAN_STATE_ADHOC_RETENTION_DAYS",),
            command_visible=False,
        ),
    ] = 30

    @field_validator("retention_days")
    @classmethod
    def _validate_retention_days(cls, value: int) -> int:
        if isinstance(value, bool) or value < -1:
            raise ValueError(
                f"Invalid value for 'state.retention_days': expected int >= -1, got {value!r}."
            )
        return value

    @field_validator("adhoc_retention_days")
    @classmethod
    def _validate_adhoc_retention_days(cls, value: int) -> int:
        if isinstance(value, bool) or value < -1:
            raise ValueError(
                "Invalid value for 'state.adhoc_retention_days': "
                f"expected int >= -1, got {value!r}."
            )
        return value


class IdleConfig(ConfigModel):
    """Cross-harness idle behavior settings."""

    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    enabled: Annotated[
        bool,
        config_field(
            "idle.enabled",
            value_kind="bool",
            file_aliases=(file_alias("idle", "enabled"),),
            env_vars=("MERIDIAN_IDLE_ENABLED",),
        ),
    ] = True
    push_seconds: Annotated[
        int,
        config_field(
            "idle.push_seconds",
            value_kind="int",
            file_aliases=(file_alias("idle", "push_seconds"),),
            env_vars=("MERIDIAN_IDLE_PUSH_SECONDS",),
        ),
        Field(ge=0),
    ] = 60
    long_turn_seconds: Annotated[
        int,
        config_field(
            "idle.long_turn_seconds",
            value_kind="int",
            file_aliases=(file_alias("idle", "long_turn_seconds"),),
            env_vars=("MERIDIAN_IDLE_LONG_TURN_SECONDS",),
        ),
        Field(ge=0),
    ] = 120
    quick_turn_push_seconds: Annotated[
        int,
        config_field(
            "idle.quick_turn_push_seconds",
            value_kind="int",
            file_aliases=(file_alias("idle", "quick_turn_push_seconds"),),
            env_vars=("MERIDIAN_IDLE_QUICK_TURN_PUSH_SECONDS",),
        ),
        Field(gt=0),
    ] = 600
    warn_minutes: Annotated[
        int,
        config_field(
            "idle.warn_minutes",
            value_kind="int",
            file_aliases=(file_alias("idle", "warn_minutes"),),
            env_vars=("MERIDIAN_IDLE_WARN_MINUTES",),
        ),
        Field(ge=0),
    ] = 15
    warn_email: Annotated[
        bool,
        config_field(
            "idle.warn_email",
            value_kind="bool",
            file_aliases=(file_alias("idle", "warn_email"),),
            env_vars=("MERIDIAN_IDLE_WARN_EMAIL",),
        ),
    ] = True
    late_fire_tolerance_seconds: Annotated[
        int,
        config_field(
            "idle.late_fire_tolerance_seconds",
            value_kind="int",
            file_aliases=(file_alias("idle", "late_fire_tolerance_seconds"),),
            env_vars=("MERIDIAN_IDLE_LATE_FIRE_TOLERANCE_SECONDS",),
        ),
        Field(ge=0),
    ] = 120


class NotifyConfig(ConfigModel):
    """Notification delivery settings shared by manual and idle notices."""

    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    push_backend: Annotated[
        str,
        config_field(
            "notify.push_backend",
            value_kind="str",
            file_aliases=(file_alias("notify", "push_backend"),),
            env_vars=("MERIDIAN_NOTIFY_PUSH_BACKEND",),
        ),
    ] = "ntfy"
    email_backend: Annotated[
        str,
        config_field(
            "notify.email_backend",
            value_kind="str",
            file_aliases=(file_alias("notify", "email_backend"),),
            env_vars=("MERIDIAN_NOTIFY_EMAIL_BACKEND",),
        ),
    ] = "gmail"
    include_messages: Annotated[
        bool,
        config_field(
            "notify.include_messages",
            value_kind="bool",
            file_aliases=(file_alias("notify", "include_messages"),),
            env_vars=("MERIDIAN_NOTIFY_INCLUDE_MESSAGES",),
        ),
    ] = True
    ntfy_server: Annotated[
        str,
        config_field(
            "notify.ntfy_server",
            value_kind="str",
            file_aliases=(file_alias("notify", "ntfy_server"),),
            env_vars=("MERIDIAN_NOTIFY_NTFY_SERVER",),
        ),
    ] = "https://ntfy.sh"
    ntfy_topic: Annotated[
        str | None,
        config_field(
            "notify.ntfy_topic",
            value_kind="str",
            file_aliases=(file_alias("notify", "ntfy_topic"),),
            env_vars=("MERIDIAN_NOTIFY_NTFY_TOPIC",),
        ),
    ] = None
    email_to: Annotated[
        str | None,
        config_field(
            "notify.email_to",
            value_kind="str",
            file_aliases=(file_alias("notify", "email_to"),),
            env_vars=("MERIDIAN_NOTIFY_EMAIL_TO",),
        ),
    ] = None
    email_from: Annotated[
        str | None,
        config_field(
            "notify.email_from",
            value_kind="str",
            file_aliases=(file_alias("notify", "email_from"),),
            env_vars=("MERIDIAN_NOTIFY_EMAIL_FROM",),
        ),
    ] = None
    smtp_user: Annotated[
        str | None,
        config_field(
            "notify.smtp_user",
            value_kind="str",
            file_aliases=(file_alias("notify", "smtp_user"),),
            env_vars=("MERIDIAN_NOTIFY_SMTP_USER",),
        ),
    ] = None
    smtp_host: Annotated[
        str,
        config_field(
            "notify.smtp_host",
            value_kind="str",
            file_aliases=(file_alias("notify", "smtp_host"),),
            env_vars=("MERIDIAN_NOTIFY_SMTP_HOST",),
        ),
    ] = "smtp.gmail.com"
    smtp_port: Annotated[
        int,
        config_field(
            "notify.smtp_port",
            value_kind="int",
            file_aliases=(file_alias("notify", "smtp_port"),),
            env_vars=("MERIDIAN_NOTIFY_SMTP_PORT",),
        ),
    ] = 587
    smtp_password_file: Annotated[
        str | None,
        config_field(
            "notify.smtp_password_file",
            value_kind="str",
            file_aliases=(file_alias("notify", "smtp_password_file"),),
            env_vars=("MERIDIAN_NOTIFY_SMTP_PASSWORD_FILE",),
        ),
    ] = None

    @field_validator("push_backend", "email_backend")
    @classmethod
    def _validate_backend(cls, value: str) -> str:
        from meridian.lib.notify.channels import normalize_channel_name

        try:
            return normalize_channel_name(value)
        except ValueError as exc:
            raise ValueError(
                f"{exc} See docs/upgrading.md#notifications-are-hook-events."
            ) from None

    @field_validator("smtp_port")
    @classmethod
    def _validate_smtp_port(cls, value: int) -> int:
        if isinstance(value, bool) or not 1 <= value <= 65535:
            raise ValueError(
                f"Invalid value for 'notify.smtp_port': expected 1..65535, got {value!r}."
            )
        return value


class WorkArtifactsConfig(ConfigModel):
    """Artifact behavior nested under the work-item settings."""

    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    sync: str | None = None

    @field_validator("sync")
    @classmethod
    def _validate_sync(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_required_string(value, source="work.artifacts.sync")


class WorkConfig(ConfigModel):
    """Work-item behavior settings."""

    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    default_worktree: bool = False
    worktree_base: str | None = None
    # Inspection-only dynamic values are not part of operational config snapshots.
    artifacts: WorkArtifactsConfig | None = Field(default=None, exclude=True)

    @field_validator("worktree_base")
    @classmethod
    def _validate_worktree_base(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_required_string(value, source="work.worktree_base")


class PrimaryConfig(ConfigModel):
    """Primary-specific harness settings."""

    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    autocompact: Annotated[
        AutocompactValue,
        config_field(
            "primary.autocompact",
            value_kind="int",
            file_aliases=(file_alias("primary", "autocompact"),),
        ),
    ] = None
    autocompact_pct: Annotated[
        AutocompactPctValue,
        config_field(
            "primary.autocompact_pct",
            value_kind="int",
            file_aliases=(file_alias("primary", "autocompact_pct"),),
        ),
    ] = None
    agent: Annotated[
        str | None,
        config_field(
            "primary.agent",
            value_kind="str",
            file_aliases=(file_alias("primary", "agent"),),
            env_vars=("MERIDIAN_AGENT",),
        ),
    ] = None
    effort: Annotated[
        EffortValue,
        config_field(
            "primary.effort",
            value_kind="str",
            file_aliases=(file_alias("primary", "effort"),),
            command_visible=False,
        ),
    ] = None
    sandbox: Annotated[
        str | None,
        config_field(
            "primary.sandbox",
            value_kind="str",
            file_aliases=(file_alias("primary", "sandbox"),),
            command_visible=False,
        ),
    ] = None
    approval: Annotated[
        ApprovalValue,
        config_field(
            "primary.approval",
            value_kind="str",
            file_aliases=(file_alias("primary", "approval"),),
            command_visible=False,
        ),
    ] = None
    timeout: float | None = None

    @model_validator(mode="before")
    @classmethod
    def _discard_deprecated_routing_fields(cls, value: object) -> object:
        if not is_json_object(value):
            return value

        normalized = dict(value)
        for field_name in ("model", "harness"):
            deprecated = normalized.pop(field_name, None)
            if deprecated is None:
                continue
            if not isinstance(deprecated, str):
                raise ValueError(
                    f"Invalid value for 'primary.{field_name}': expected str, got "
                    f"{type(deprecated).__name__} ({deprecated!r})."
                )
            normalize_required_string(deprecated, source=f"primary.{field_name}")
        return normalized

    @field_validator("agent", "effort", "sandbox", "approval", mode="before")
    @classmethod
    def _validate_string_fields(cls, value: object, info: ValidationInfo) -> object:
        if value is None or not isinstance(value, str):
            return value
        return normalize_required_string(value, source=f"primary.{info.field_name}")

    @field_validator("timeout")
    @classmethod
    def _validate_timeout(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or value <= 0:
            raise ValueError(
                f"Invalid value for 'primary.timeout': expected float > 0, got {value!r}."
            )
        return value


class EnvironmentConfig(ConfigModel):
    model_config = ConfigDict(frozen=True, strict=True)

    ignore_direnv: Annotated[
        bool,
        config_field(
            "environment.ignore_direnv",
            value_kind="bool",
            file_aliases=(file_alias("environment", "ignore_direnv"),),
            env_vars=("MERIDIAN_IGNORE_DIRENV",),
        ),
    ] = False


_TIMEOUT_FIELD_NAMES = {
    "guardrail_minutes": "guardrail_timeout_minutes",
    "startup_minutes": "startup_timeout_minutes",
    "wait_minutes": "wait_timeout_minutes",
}


def _timeout_field(name: str, kind: ValueKind) -> ConfigOptionMeta:
    """Describe one timeout option and its accepted legacy field spelling."""

    field_name = _TIMEOUT_FIELD_NAMES.get(name, name)
    aliases = [file_alias("timeouts", name)]
    if field_name != name:
        aliases.append(file_alias("timeouts", field_name))
    aliases.append(file_alias(None, field_name))
    return config_field(
        f"timeouts.{name}",
        value_kind=kind,
        file_aliases=tuple(aliases),
        env_vars=(f"MERIDIAN_{field_name.upper()}",),
    )


class ExperimentalConfig(ConfigModel):
    """Opt-in behavior switches, kept default-stable until an experiment settles."""

    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    skill_delivery: Annotated[
        SkillDeliveryMode,
        config_field(
            "experimental.skill_delivery",
            value_kind="str",
            file_aliases=(file_alias("experimental", "skill_delivery"),),
            env_vars=("MERIDIAN_EXPERIMENTAL_SKILL_DELIVERY",),
        ),
    ] = "inline"


class MeridianConfig(ConfigModel):
    """Resolved operational configuration for meridian."""

    model_config = ConfigDict(
        frozen=True,
        extra="ignore",
        strict=True,
    )

    max_depth: Annotated[
        int,
        config_field(
            "defaults.max_depth",
            value_kind="int",
            file_aliases=(file_alias("defaults", "max_depth"), file_alias(None, "max_depth")),
            env_vars=("MERIDIAN_MAX_DEPTH",),
        ),
    ] = 3
    kill_grace_minutes: Annotated[
        float,
        _timeout_field("kill_grace_minutes", "float"),
    ] = 2.0 / 60.0
    guardrail_timeout_minutes: Annotated[
        float,
        _timeout_field("guardrail_minutes", "float"),
    ] = 0.5
    wait_timeout_minutes: Annotated[
        float,
        _timeout_field("wait_minutes", "float"),
    ] = 30.0
    startup_timeout_minutes: Annotated[
        float,
        _timeout_field("startup_minutes", "float"),
    ] = 5.0
    pi_child_wave_timeout_seconds: Annotated[
        float | None,
        _timeout_field("pi_child_wave_timeout_seconds", "float"),
    ] = None
    resident_deadline_seconds: Annotated[
        float | None,
        _timeout_field("resident_deadline_seconds", "float"),
    ] = None
    resident_poll_seconds: Annotated[
        float | None,
        _timeout_field("resident_poll_seconds", "float"),
    ] = None
    resident_rearm_budget: Annotated[
        int | None,
        _timeout_field("resident_rearm_budget", "int"),
    ] = None

    @field_validator("resident_rearm_budget")
    @classmethod
    def _validate_resident_rearm_budget(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or value < 0:
            raise ValueError(
                "Invalid value for 'timeouts.resident_rearm_budget': "
                f"expected int >= 0, got {value!r}."
            )
        return value

    pi_task_ping_interval_seconds: Annotated[
        float | None,
        _timeout_field("pi_task_ping_interval_seconds", "float"),
    ] = None
    default_wait_yield_seconds: Annotated[
        float,
        config_field(
            "spawn.default_wait_yield_seconds",
            value_kind="float",
            file_aliases=(file_alias("spawn", "default_wait_yield_seconds"),),
            env_vars=("MERIDIAN_DEFAULT_WAIT_YIELD_SECONDS",),
        ),
    ] = 3000.0
    min_wait_yield_seconds: Annotated[
        float,
        config_field(
            "spawn.min_wait_yield_seconds",
            value_kind="float",
            file_aliases=(file_alias("spawn", "min_wait_yield_seconds"),),
            env_vars=("MERIDIAN_MIN_WAIT_YIELD_SECONDS",),
        ),
    ] = 30.0
    deny_headless_harnesses: Annotated[
        tuple[str, ...],
        config_field(
            "spawn.deny_headless_harnesses",
            value_kind="str_list",
            file_aliases=(file_alias("spawn", "deny_headless_harnesses"),),
        ),
    ] = ()
    harness: HarnessConfig = Field(default_factory=HarnessConfig)
    idle: IdleConfig = Field(default_factory=IdleConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    primary: PrimaryConfig = Field(default_factory=PrimaryConfig)
    history: HistoryConfig = Field(default_factory=HistoryConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    state: StateConfig = Field(default_factory=StateConfig)
    work: WorkConfig = Field(default_factory=WorkConfig)
    env: dict[str, str] = Field(default_factory=dict)
    environment: EnvironmentConfig = Field(default_factory=EnvironmentConfig)
    experimental: ExperimentalConfig = Field(default_factory=ExperimentalConfig)

    @field_validator("deny_headless_harnesses", mode="before")
    @classmethod
    def _validate_deny_headless_harnesses(cls, value: object) -> tuple[str, ...]:
        return normalize_string_sequence(
            value,
            source="spawn.deny_headless_harnesses",
            empty_entries="items",
        )

    @field_validator("env")
    @classmethod
    def _validate_env(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not key.strip() for key in value):
            raise ValueError("expected non-empty key")
        return value

    @field_validator("default_wait_yield_seconds", "min_wait_yield_seconds")
    @classmethod
    def _validate_wait_yield_settings(cls, value: float) -> float:
        if isinstance(value, bool) or value <= 0:
            raise ValueError(f"Invalid wait-yield setting: expected float > 0, got {value!r}.")
        return float(value)

    @field_validator("startup_timeout_minutes")
    @classmethod
    def _validate_startup_timeout_minutes(cls, value: float) -> float:
        if isinstance(value, bool) or value <= 0:
            raise ValueError(f"Invalid startup timeout: expected float > 0, got {value!r}.")
        return float(value)

    def wait_yield_seconds_for_harness(self, harness_id: str | None) -> float:
        """Return clamped wait-yield seconds for a harness or the unknown default."""

        configured = self.harness_profile(harness_id)
        raw_value = (
            configured.wait_yield_seconds
            if configured is not None and configured.wait_yield_seconds is not None
            else self.default_wait_yield_seconds
        )
        return max(float(raw_value), float(self.min_wait_yield_seconds))

    def harness_profile(self, harness_id: str | None) -> HarnessProfileConfig | None:
        """Return the typed profile for a supported harness ID."""

        normalized = (harness_id or "").strip().lower()
        if normalized not in _HARNESS_TABLE_KEYS:
            return None
        profile = getattr(self.harness, normalized, None)
        if not isinstance(profile, HarnessProfileConfig):
            return None
        return profile
