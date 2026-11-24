from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, Any, ClassVar

from pydantic import ConfigDict, Field, create_model, field_validator, model_validator
from typing_extensions import TypeIs

from meridian.lib.config.catalog import file_alias
from meridian.lib.config.model_base import ConfigModel
from meridian.lib.config.schema import ValueKind, config_field
from meridian.lib.utils.json_shapes import json_list


def _is_object_tuple(value: object) -> TypeIs[tuple[object, ...]]:
    return isinstance(value, tuple)


class HarnessIdleConfig(ConfigModel):
    """Shared typed fields for every harness-specific idle config."""

    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    enabled: bool = True
    ttl_seconds: Annotated[int | None, Field(gt=0)] = None
    warn_minutes: Annotated[int, Field(ge=0)] = 10
    keepwarm: bool = False
    keepwarm_pings: int = 3
    state_reminder: bool = True


class HarnessProfileConfig(ConfigModel):
    """Per-harness model and wait-yield settings."""

    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    model: str = ""
    wait_yield_seconds: float | None = None
    idle: HarnessIdleConfig = Field(default_factory=HarnessIdleConfig)
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


def harness_idle_model(
    harness_id: str,
    *,
    defaults: Mapping[str, object] | None = None,
) -> type[HarnessIdleConfig]:

    resolved_defaults = dict(defaults or {})
    unknown_defaults = resolved_defaults.keys() - HarnessIdleConfig.model_fields.keys()
    if unknown_defaults:
        names = ", ".join(sorted(unknown_defaults))
        raise ValueError(f"Unsupported idle defaults for {harness_id}: {names}.")

    fields: dict[str, Any] = {}
    value_kinds: dict[str, ValueKind] = {
        "enabled": "bool",
        "ttl_seconds": "int",
        "warn_minutes": "int",
        "keepwarm": "bool",
        "keepwarm_pings": "int",
        "state_reminder": "bool",
    }
    for field_name, model_field in HarnessIdleConfig.model_fields.items():
        option = config_field(
            f"harness.{harness_id}.idle.{field_name}",
            value_kind=value_kinds[field_name],
            file_aliases=(file_alias(("harness", harness_id, "idle"), field_name),),
            env_vars=(f"MERIDIAN_HARNESS_IDLE_{field_name.upper()}_{harness_id.upper()}",),
        )
        annotation = Annotated[(model_field.annotation, *model_field.metadata, option)]
        fields[field_name] = (
            annotation,
            resolved_defaults.get(field_name, model_field.default),
        )

    display_name = "OpenCode" if harness_id == "opencode" else harness_id.title()
    return create_model(
        f"{display_name}HarnessIdleConfig",
        __base__=HarnessIdleConfig,
        __module__=__name__,
        **fields,
    )


class OpenCodeHarnessProfileBase(HarnessProfileConfig):
    """OpenCode-only profile fields used by the generated harness model."""

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


class BundleToggleConfig(ConfigModel):
    """Enable switch shared by harness-owned runtime bundles."""

    config_silences_extras: ClassVar[bool] = True

    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    enabled: bool = True


class ClaudeHarnessProfileBase(HarnessProfileConfig):
    """Claude-only profile fields used by the generated harness model."""

    spawn_watch: BundleToggleConfig = Field(default_factory=BundleToggleConfig)


class PiHarnessProfileBase(HarnessProfileConfig):
    """Pi-only profile fields used by the generated harness model."""

    load_all_pi_extensions: bool = False
    extra_extension_paths: tuple[str, ...] = ()
    background_tasks: BundleToggleConfig = Field(default_factory=BundleToggleConfig)
    spawn_watch: BundleToggleConfig = Field(default_factory=BundleToggleConfig)
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


def harness_profile_model(
    harness_id: str,
    *,
    base: type[HarnessProfileConfig] = HarnessProfileConfig,
    model_default: str = "",
    idle_defaults: Mapping[str, object] | None = None,
    idle_only: bool = False,
) -> type[HarnessProfileConfig]:
    """A profile model. ``idle_only`` registers just ``[harness.<id>.idle]``, for a harness
    Meridian does not launch, whose default model and wait yield nothing would read."""

    idle_model = harness_idle_model(harness_id, defaults=idle_defaults)
    fields: dict[str, Any] = {"idle": (idle_model, Field(default_factory=idle_model))}
    if not idle_only:
        fields["wait_yield_seconds"] = (
            Annotated[
                float | None,
                config_field(
                    f"harness.{harness_id}.wait_yield_seconds",
                    value_kind="float",
                    file_aliases=(file_alias(("harness", harness_id), "wait_yield_seconds"),),
                    env_vars=(f"MERIDIAN_HARNESS_WAIT_YIELD_SECONDS_{harness_id.upper()}",),
                ),
            ],
            None,
        )
    if not idle_only and harness_id != "pi":
        fields["model"] = (
            Annotated[
                str,
                config_field(
                    f"harness.{harness_id}",
                    value_kind="str",
                    file_aliases=(
                        file_alias(("harness", harness_id), "model"),
                        file_alias("harness", harness_id),
                    ),
                    env_vars=(f"MERIDIAN_HARNESS_MODEL_{harness_id.upper()}",),
                ),
            ],
            model_default,
        )

    display_name = "OpenCode" if harness_id == "opencode" else harness_id.title()
    return create_model(
        f"{display_name}HarnessProfileConfig",
        __base__=base,
        __module__=__name__,
        **fields,
    )


if TYPE_CHECKING:
    ClaudeHarnessProfileConfig = ClaudeHarnessProfileBase
    CodexHarnessProfileConfig = HarnessProfileConfig
    OpenCodeHarnessProfileConfig = OpenCodeHarnessProfileBase
    PiHarnessProfileConfig = PiHarnessProfileBase
    AgyHarnessProfileConfig = HarnessProfileConfig
else:
    ClaudeHarnessProfileConfig = harness_profile_model("claude", base=ClaudeHarnessProfileBase)
    CodexHarnessProfileConfig = harness_profile_model(
        "codex",
        idle_defaults={"ttl_seconds": 1800},
    )
    OpenCodeHarnessProfileConfig = harness_profile_model(
        "opencode",
        base=OpenCodeHarnessProfileBase,
        model_default="opencode-go/kimi-k2.6",
    )
    PiHarnessProfileConfig = harness_profile_model("pi", base=PiHarnessProfileBase)
    AgyHarnessProfileConfig = harness_profile_model("agy", idle_only=True)


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
    agy: AgyHarnessProfileConfig = Field(default_factory=AgyHarnessProfileConfig)
