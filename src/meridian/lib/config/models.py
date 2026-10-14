"""Typed models for Meridian operational configuration."""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, Any, ClassVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    create_model,
    field_validator,
    model_validator,
)
from typing_extensions import TypeIs

from meridian.lib.config.catalog import file_alias
from meridian.lib.config.schema import config_field
from meridian.lib.core.overrides import (
    ApprovalValue,
    AutocompactPctValue,
    AutocompactValue,
    EffortValue,
)
from meridian.lib.utils.json_shapes import is_json_object, json_list

_OUTPUT_VERBOSITY_PRESETS = frozenset({"quiet", "normal", "verbose", "debug"})
_HARNESS_TABLE_KEYS = frozenset({"claude", "codex", "opencode", "pi"})


class ConfigModel(BaseModel):
    """Base for models carrying file-key warning policy."""

    _config_ignored_keys: ClassVar[frozenset[str]] = frozenset()
    _config_silent_extras: ClassVar[bool] = False

    @classmethod
    def config_ignored_keys(cls) -> frozenset[str]:
        return cls._config_ignored_keys

    @classmethod
    def config_silences_extras(cls) -> bool:
        return cls._config_silent_extras


def normalize_required_string(raw: str, *, source: str) -> str:
    normalized = raw.strip()
    if not normalized:
        raise ValueError(f"Invalid value for '{source}': expected non-empty string.")
    return normalized


def _is_object_tuple(value: object) -> TypeIs[tuple[object, ...]]:
    return isinstance(value, tuple)


def normalize_string_sequence(
    value: object,
    *,
    source: str,
    empty_entries: str = "entries",
) -> tuple[str, ...]:
    values = json_list(value)
    if values is None:
        if not _is_object_tuple(value):
            raise ValueError(
                f"Invalid value for '{source}': expected array[str], "
                f"got {type(value).__name__} ({value!r})."
            )
        values = value

    normalized: list[str] = []
    for item in values:
        if not isinstance(item, str):
            raise ValueError(
                f"Invalid value for '{source}': expected array[str], "
                f"got {type(item).__name__} ({item!r})."
            )
        compact = item.strip()
        if not compact:
            raise ValueError(
                f"Invalid value for '{source}': expected non-empty {empty_entries}."
            )
        normalized.append(compact)
    return tuple(normalized)


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

    @field_validator("retention_days")
    @classmethod
    def _validate_retention_days(cls, value: int) -> int:
        if isinstance(value, bool) or value < -1:
            raise ValueError(
                f"Invalid value for 'state.retention_days': expected int >= -1, got {value!r}."
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
    compact_minutes: Annotated[
        int,
        config_field(
            "idle.compact_minutes",
            value_kind="int",
            file_aliases=(file_alias("idle", "compact_minutes"),),
            env_vars=("MERIDIAN_IDLE_COMPACT_MINUTES",),
        ),
        Field(ge=0),
    ] = 5
    compact: Annotated[
        bool,
        config_field(
            "idle.compact",
            value_kind="bool",
            file_aliases=(file_alias("idle", "compact"),),
            env_vars=("MERIDIAN_IDLE_COMPACT",),
        ),
    ] = True
    min_compact_tokens: Annotated[
        int,
        config_field(
            "idle.min_compact_tokens",
            value_kind="int",
            file_aliases=(file_alias("idle", "min_compact_tokens"),),
            env_vars=("MERIDIAN_IDLE_MIN_COMPACT_TOKENS",),
        ),
        Field(ge=0),
    ] = 40_000
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
    push_command: Annotated[
        str | None,
        config_field(
            "notify.push_command",
            value_kind="str",
            file_aliases=(file_alias("notify", "push_command"),),
            env_vars=("MERIDIAN_NOTIFY_PUSH_COMMAND",),
        ),
    ] = None
    email_command: Annotated[
        str | None,
        config_field(
            "notify.email_command",
            value_kind="str",
            file_aliases=(file_alias("notify", "email_command"),),
            env_vars=("MERIDIAN_NOTIFY_EMAIL_COMMAND",),
        ),
    ] = None

    @field_validator("smtp_port")
    @classmethod
    def _validate_smtp_port(cls, value: int) -> int:
        if isinstance(value, bool) or not 1 <= value <= 65535:
            raise ValueError(
                f"Invalid value for 'notify.smtp_port': expected 1..65535, got {value!r}."
            )
        return value


class WorkConfig(ConfigModel):
    """Work-item behavior settings."""

    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    default_worktree: bool = False
    worktree_base: str | None = None


class PrimaryConfig(ConfigModel):
    """Primary-specific harness settings."""

    _config_ignored_keys: ClassVar[frozenset[str]] = frozenset({"model", "harness"})

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


class HarnessProfileConfig(ConfigModel):
    """Per-harness model and wait-yield settings."""

    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    model: str = ""
    wait_yield_seconds: float | None = None
    # Harness-native subagents (Claude built-in Agent types, Codex multi-agent)
    # spawn work Meridian cannot see; Meridian is the orchestrator by default.
    allow_builtin_agents: bool = False

    @model_validator(mode="before")
    @classmethod
    def _string_as_model(cls, values: object) -> object:
        if isinstance(values, str):
            return {"model": values}
        return values

    @field_validator("model")
    @classmethod
    def _normalize_model(cls, value: str) -> str:
        return value.strip()

    @field_validator("wait_yield_seconds")
    @classmethod
    def _validate_wait_yield_seconds(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError(
                f"Invalid value for 'harness.wait_yield_seconds': expected float, got {value!r}."
            )
        return float(value)


class _HarnessIdleFields(ConfigModel):
    """Shared typed fields for every harness-specific idle config."""

    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    enabled: bool = True
    compact: bool = True
    ttl_seconds: Annotated[int | None, Field(gt=0)] = None


def harness_idle_model(
    harness_id: str,
    *,
    defaults: Mapping[str, object] | None = None,
) -> type[_HarnessIdleFields]:
    """Build the metadata-backed idle settings model for one harness."""

    normalized = harness_id.strip().lower()
    if normalized not in _HARNESS_TABLE_KEYS:
        raise ValueError(f"Unsupported harness ID for idle config: {harness_id!r}.")
    resolved_defaults = dict(defaults or {})
    unknown_defaults = resolved_defaults.keys() - _HarnessIdleFields.model_fields.keys()
    if unknown_defaults:
        names = ", ".join(sorted(unknown_defaults))
        raise ValueError(f"Unsupported idle defaults for {normalized}: {names}.")

    fields: dict[str, Any] = {}
    for field_name, model_field in _HarnessIdleFields.model_fields.items():
        option = config_field(
            f"harness.{normalized}.idle.{field_name}",
            value_kind="int" if field_name == "ttl_seconds" else "bool",
            file_aliases=(
                file_alias(("harness", normalized, "idle"), field_name),
            ),
            env_vars=(
                f"MERIDIAN_HARNESS_IDLE_{field_name.upper()}_{normalized.upper()}",
            ),
        )
        annotation = Annotated[(model_field.annotation, *model_field.metadata, option)]
        fields[field_name] = (
            annotation,
            resolved_defaults.get(field_name, model_field.default),
        )

    return create_model(
        f"{normalized.title()}HarnessIdleConfig",
        __base__=_HarnessIdleFields,
        __module__=__name__,
        **fields,
    )


if TYPE_CHECKING:
    ClaudeHarnessIdleConfig = _HarnessIdleFields
    CodexHarnessIdleConfig = _HarnessIdleFields
    OpenCodeHarnessIdleConfig = _HarnessIdleFields
    PiHarnessIdleConfig = _HarnessIdleFields
else:
    ClaudeHarnessIdleConfig = harness_idle_model("claude")
    CodexHarnessIdleConfig = harness_idle_model("codex", defaults={"ttl_seconds": 1800})
    OpenCodeHarnessIdleConfig = harness_idle_model(
        "opencode",
        defaults={"ttl_seconds": 300},
    )
    PiHarnessIdleConfig = harness_idle_model("pi")


class ClaudeHarnessProfileConfig(HarnessProfileConfig):
    idle: ClaudeHarnessIdleConfig = Field(default_factory=ClaudeHarnessIdleConfig)
    model: Annotated[
        str,
        config_field(
            "harness.claude",
            value_kind="str",
            file_aliases=(
                file_alias("harness", "claude"),
                file_alias(("harness", "claude"), "model"),
            ),
            env_vars=("MERIDIAN_HARNESS_MODEL_CLAUDE",),
        ),
    ] = ""


class CodexHarnessProfileConfig(HarnessProfileConfig):
    idle: CodexHarnessIdleConfig = Field(default_factory=CodexHarnessIdleConfig)
    model: Annotated[
        str,
        config_field(
            "harness.codex",
            value_kind="str",
            file_aliases=(
                file_alias("harness", "codex"),
                file_alias(("harness", "codex"), "model"),
            ),
            env_vars=("MERIDIAN_HARNESS_MODEL_CODEX",),
        ),
    ] = ""


class OpenCodeHarnessProfileConfig(HarnessProfileConfig):
    idle: OpenCodeHarnessIdleConfig = Field(default_factory=OpenCodeHarnessIdleConfig)
    model: Annotated[
        str,
        config_field(
            "harness.opencode",
            value_kind="str",
            file_aliases=(
                file_alias("harness", "opencode"),
                file_alias(("harness", "opencode"), "model"),
            ),
            env_vars=("MERIDIAN_HARNESS_MODEL_OPENCODE",),
        ),
    ] = "opencode-go/kimi-k2.6"
    version: Annotated[
        str,
        config_field(
            "harness.opencode.version",
            value_kind="str",
            file_aliases=(file_alias(("harness", "opencode"), "version"),),
            env_vars=("MERIDIAN_HARNESS_OPENCODE_VERSION",),
        ),
    ] = "auto"

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in ("auto", "v1", "v2"):
            raise ValueError(
                "Invalid value for 'harness.opencode.version': expected one of "
                f"'auto', 'v1', 'v2', got {value!r}."
            )
        return normalized


class PiBundleToggleConfig(ConfigModel):
    """Per-bundle enable switch under ``[harness.pi]``."""

    _config_silent_extras: ClassVar[bool] = True

    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    enabled: bool = True


class PiHarnessProfileConfig(HarnessProfileConfig):
    idle: PiHarnessIdleConfig = Field(default_factory=PiHarnessIdleConfig)
    load_all_pi_extensions: bool = False
    extra_extension_paths: tuple[str, ...] = ()
    background_tasks: PiBundleToggleConfig = Field(default_factory=PiBundleToggleConfig)
    spawn_watch: PiBundleToggleConfig = Field(default_factory=PiBundleToggleConfig)
    disable_managed_bash: bool = False

    @field_validator("extra_extension_paths", mode="before")
    @classmethod
    def _normalize_extra_extension_paths(cls, value: object) -> tuple[str, ...]:
        items = json_list(value)
        if items is None:
            if not _is_object_tuple(value):
                raise ValueError(
                    "Invalid value for 'harness.pi.extra_extension_paths': expected array, "
                    f"got {type(value).__name__} ({value!r})."
                )
            items = value
        return tuple(normalized for item in items if (normalized := str(item).strip()))

    def background_tasks_enabled(self) -> bool:
        if self.disable_managed_bash:
            return False
        return self.background_tasks.enabled


class HarnessConfig(ConfigModel):
    """Default model and wait-yield configuration for each harness adapter."""

    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    claude: ClaudeHarnessProfileConfig = Field(
        default_factory=lambda: ClaudeHarnessProfileConfig(wait_yield_seconds=3000.0)
    )
    codex: CodexHarnessProfileConfig = Field(
        default_factory=lambda: CodexHarnessProfileConfig(wait_yield_seconds=3000.0)
    )
    opencode: OpenCodeHarnessProfileConfig = Field(default_factory=OpenCodeHarnessProfileConfig)
    pi: PiHarnessProfileConfig = Field(default_factory=PiHarnessProfileConfig)


class HistoryArchiveConfig(ConfigModel):
    # This section predated the strict TOML normalizers and historically accepted
    # Pydantic's scalar coercions. Keep that file behavior while still forbidding extras.
    model_config = ConfigDict(frozen=True, extra="forbid")
    automatic: Annotated[
        bool,
        config_field(
            "history.archive.automatic",
            value_kind="bool",
            file_aliases=(file_alias(("history", "archive"), "automatic"),),
            env_vars=("MERIDIAN_HISTORY_ARCHIVE_AUTOMATIC",),
        ),
    ] = False
    after_days: Annotated[
        int,
        config_field(
            "history.archive.after_days",
            value_kind="int",
            file_aliases=(file_alias(("history", "archive"), "after_days"),),
            env_vars=("MERIDIAN_HISTORY_ARCHIVE_AFTER_DAYS",),
        ),
    ] = Field(default=30, ge=0)
    interval_hours: Annotated[
        int,
        config_field(
            "history.archive.interval_hours",
            value_kind="int",
            file_aliases=(file_alias(("history", "archive"), "interval_hours"),),
        ),
    ] = Field(default=24, ge=0)
    max_records: Annotated[
        int,
        config_field(
            "history.archive.max_records",
            value_kind="int",
            file_aliases=(file_alias(("history", "archive"), "max_records"),),
        ),
    ] = Field(default=256, ge=1, le=100_000)
    max_uncompressed_bytes: Annotated[
        int,
        config_field(
            "history.archive.max_uncompressed_bytes",
            value_kind="int",
            file_aliases=(file_alias(("history", "archive"), "max_uncompressed_bytes"),),
        ),
    ] = Field(default=1024**3, ge=1, le=1024**4)
    destination: Annotated[
        str | None,
        config_field(
            "history.archive.destination",
            value_kind="str",
            file_aliases=(file_alias(("history", "archive"), "destination"),),
            env_vars=("MERIDIAN_HISTORY_ARCHIVE_DESTINATION",),
        ),
    ] = None


class HistoryConfig(ConfigModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    archive: HistoryArchiveConfig = Field(default_factory=HistoryArchiveConfig)


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


class MeridianConfigModel(ConfigModel):
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
        config_field(
            "timeouts.kill_grace_minutes",
            value_kind="float",
            file_aliases=(
                file_alias("timeouts", "kill_grace_minutes"),
                file_alias(None, "kill_grace_minutes"),
            ),
            env_vars=("MERIDIAN_KILL_GRACE_MINUTES",),
        ),
    ] = 2.0 / 60.0
    guardrail_timeout_minutes: Annotated[
        float,
        config_field(
            "timeouts.guardrail_minutes",
            value_kind="float",
            file_aliases=(
                file_alias("timeouts", "guardrail_minutes"),
                file_alias("timeouts", "guardrail_timeout_minutes"),
                file_alias(None, "guardrail_timeout_minutes"),
            ),
            env_vars=("MERIDIAN_GUARDRAIL_TIMEOUT_MINUTES",),
        ),
    ] = 0.5
    wait_timeout_minutes: Annotated[
        float,
        config_field(
            "timeouts.wait_minutes",
            value_kind="float",
            file_aliases=(
                file_alias("timeouts", "wait_minutes"),
                file_alias("timeouts", "wait_timeout_minutes"),
                file_alias(None, "wait_timeout_minutes"),
            ),
            env_vars=("MERIDIAN_WAIT_TIMEOUT_MINUTES",),
        ),
    ] = 30.0
    startup_timeout_minutes: Annotated[
        float,
        config_field(
            "timeouts.startup_minutes",
            value_kind="float",
            file_aliases=(
                file_alias("timeouts", "startup_minutes"),
                file_alias("timeouts", "startup_timeout_minutes"),
                file_alias(None, "startup_timeout_minutes"),
            ),
            env_vars=("MERIDIAN_STARTUP_TIMEOUT_MINUTES",),
        ),
    ] = 5.0
    pi_child_wave_timeout_seconds: Annotated[
        float | None,
        config_field(
            "timeouts.pi_child_wave_timeout_seconds",
            value_kind="float",
            file_aliases=(
                file_alias("timeouts", "pi_child_wave_timeout_seconds"),
                file_alias(None, "pi_child_wave_timeout_seconds"),
            ),
            env_vars=("MERIDIAN_PI_CHILD_WAVE_TIMEOUT_SECONDS",),
        ),
    ] = None
    resident_deadline_seconds: Annotated[
        float | None,
        config_field(
            "timeouts.resident_deadline_seconds",
            value_kind="float",
            file_aliases=(
                file_alias("timeouts", "resident_deadline_seconds"),
                file_alias(None, "resident_deadline_seconds"),
            ),
            env_vars=("MERIDIAN_RESIDENT_DEADLINE_SECONDS",),
        ),
    ] = None
    resident_poll_seconds: Annotated[
        float | None,
        config_field(
            "timeouts.resident_poll_seconds",
            value_kind="float",
            file_aliases=(
                file_alias("timeouts", "resident_poll_seconds"),
                file_alias(None, "resident_poll_seconds"),
            ),
            env_vars=("MERIDIAN_RESIDENT_POLL_SECONDS",),
        ),
    ] = None
    resident_rearm_budget: Annotated[
        int | None,
        config_field(
            "timeouts.resident_rearm_budget",
            value_kind="int",
            file_aliases=(
                file_alias("timeouts", "resident_rearm_budget"),
                file_alias(None, "resident_rearm_budget"),
            ),
            env_vars=("MERIDIAN_RESIDENT_REARM_BUDGET",),
        ),
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
        config_field(
            "timeouts.pi_task_ping_interval_seconds",
            value_kind="float",
            file_aliases=(
                file_alias("timeouts", "pi_task_ping_interval_seconds"),
                file_alias(None, "pi_task_ping_interval_seconds"),
            ),
            env_vars=("MERIDIAN_PI_TASK_PING_INTERVAL_SECONDS",),
        ),
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

    @field_validator("deny_headless_harnesses", mode="before")
    @classmethod
    def _validate_deny_headless_harnesses(cls, value: object) -> tuple[str, ...]:
        return normalize_string_sequence(
            value,
            source="spawn.deny_headless_harnesses",
            empty_entries="items",
        )

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

    def default_model_for_harness(self, harness_id: str) -> str | None:
        """Return configured default model for one harness ID."""

        profile = self._harness_profile_for_id(harness_id)
        return None if profile is None else profile.model

    def wait_yield_seconds_for_harness(self, harness_id: str | None) -> float:
        """Return clamped wait-yield seconds for a harness or the unknown default."""

        configured = self._harness_profile_for_id(harness_id)
        raw_value = (
            configured.wait_yield_seconds
            if configured is not None and configured.wait_yield_seconds is not None
            else self.default_wait_yield_seconds
        )
        return max(float(raw_value), float(self.min_wait_yield_seconds))

    def _harness_profile_for_id(self, harness_id: str | None) -> HarnessProfileConfig | None:
        normalized = (harness_id or "").strip().lower()
        if normalized not in _HARNESS_TABLE_KEYS:
            return None
        profile = getattr(self.harness, normalized, None)
        if not isinstance(profile, HarnessProfileConfig):
            return None
        return profile


class MeridianConfig(MeridianConfigModel):
    """Resolved operational configuration for Meridian."""
