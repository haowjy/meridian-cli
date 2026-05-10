"""Shared runtime override fields and layer resolution helpers."""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, field_validator

if TYPE_CHECKING:
    from meridian.lib.catalog.agent import AgentProfile
    from meridian.lib.catalog.model_aliases import AliasEntry
    from meridian.lib.config.settings import AgentOverlayConfig, MeridianConfig
    from meridian.lib.ops.spawn.models import SpawnCreateInput

_AUTOCOMPACT_TOKEN_MIN = 1000
_AUTOCOMPACT_PCT_MIN = 1
_AUTOCOMPACT_PCT_MAX = 100
_ROUTING_OVERRIDE_FIELDS = frozenset({"model", "harness", "agent"})
type ExecutionPolicyField = Literal[
    "effort", "sandbox", "approval", "autocompact", "autocompact_pct", "timeout"
]
EXECUTION_POLICY_FIELDS: tuple[ExecutionPolicyField, ...] = (
    "effort",
    "sandbox",
    "approval",
    "autocompact",
    "autocompact_pct",
    "timeout",
)
_EXECUTION_POLICY_FIELD_SET = frozenset(EXECUTION_POLICY_FIELDS)

KNOWN_EFFORT_VALUES = frozenset({"low", "medium", "high", "xhigh"})


def _validate_autocompact_value(v: object) -> object:
    """Shared validator: reject bool, enforce minimum token count."""
    if isinstance(v, bool):
        raise ValueError("autocompact must be an integer, not a boolean")
    if v is None:
        return None
    if not isinstance(v, int) or v < _AUTOCOMPACT_TOKEN_MIN:
        raise ValueError(
            f"expected int >= {_AUTOCOMPACT_TOKEN_MIN} (token count), got {v!r}."
        )
    return v


def _validate_autocompact_pct_value(v: object) -> object:
    """Shared validator: reject bool, enforce 1-100 range."""
    if isinstance(v, bool):
        raise ValueError("autocompact_pct must be an integer, not a boolean")
    if v is None:
        return None
    if not isinstance(v, int) or not (_AUTOCOMPACT_PCT_MIN <= v <= _AUTOCOMPACT_PCT_MAX):
        raise ValueError(
            f"expected int between {_AUTOCOMPACT_PCT_MIN} and {_AUTOCOMPACT_PCT_MAX}, got {v!r}."
        )
    return v


AutocompactValue = Annotated[int | None, BeforeValidator(_validate_autocompact_value)]
AutocompactPctValue = Annotated[int | None, BeforeValidator(_validate_autocompact_pct_value)]
KNOWN_APPROVAL_VALUES = frozenset({"default", "confirm", "auto", "yolo"})


def _validate_effort_value(v: object) -> object:
    """Shared validator: normalize and check effort against known values."""
    if v is None:
        return None
    if not isinstance(v, str):
        raise ValueError(f"effort must be a string, got {type(v).__name__}")
    normalized = v.strip()
    if not normalized:
        return None
    if normalized not in KNOWN_EFFORT_VALUES:
        raise ValueError(
            f"expected one of {sorted(KNOWN_EFFORT_VALUES)}, got {v!r}"
        )
    return normalized


def _validate_approval_value(v: object) -> object:
    """Shared validator: normalize and check approval against known values."""
    if v is None:
        return None
    if not isinstance(v, str):
        raise ValueError(f"approval must be a string, got {type(v).__name__}")
    normalized = v.strip()
    if not normalized:
        return None
    if normalized not in KNOWN_APPROVAL_VALUES:
        raise ValueError(
            f"expected one of {sorted(KNOWN_APPROVAL_VALUES)}, got {v!r}"
        )
    return normalized


EffortValue = Annotated[str | None, BeforeValidator(_validate_effort_value)]
ApprovalValue = Annotated[str | None, BeforeValidator(_validate_approval_value)]


RUNTIME_OVERRIDE_ENV_BY_FIELD: dict[str, str] = {
    "model": "MERIDIAN_MODEL",
    "agent": "MERIDIAN_AGENT",
    "effort": "MERIDIAN_EFFORT",
    "sandbox": "MERIDIAN_SANDBOX",
    "approval": "MERIDIAN_APPROVAL",
    "autocompact": "MERIDIAN_AUTOCOMPACT",
    "autocompact_pct": "MERIDIAN_AUTOCOMPACT_PCT",
    "timeout": "MERIDIAN_TIMEOUT",
}
RUNTIME_OVERRIDE_ENV_VARS = frozenset(RUNTIME_OVERRIDE_ENV_BY_FIELD.values())


def _parse_env_int(raw_value: str, *, env_name: str) -> int:
    try:
        return int(raw_value.strip())
    except ValueError as error:
        raise ValueError(
            f"Invalid environment override '{env_name}': expected int, got {raw_value!r}."
        ) from error


def _parse_env_float(raw_value: str, *, env_name: str) -> float:
    try:
        return float(raw_value.strip())
    except ValueError as error:
        raise ValueError(
            f"Invalid environment override '{env_name}': expected float, got {raw_value!r}."
        ) from error


def _read_env_string(env_name: str) -> str | None:
    raw_value = os.getenv(env_name)
    if raw_value is None:
        return None
    normalized = raw_value.strip()
    if not normalized:
        return None
    return normalized


def _normalize_optional_string(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return normalized


def normalize_execution_policy_fields(
    fields: Iterable[str],
) -> tuple[ExecutionPolicyField, ...]:
    """Validate and normalize execution-policy field names."""

    normalized: list[ExecutionPolicyField] = []
    unknown: list[str] = []
    seen: set[ExecutionPolicyField] = set()

    for raw_field in fields:
        field_name = raw_field.strip()
        if field_name not in _EXECUTION_POLICY_FIELD_SET:
            unknown.append(str(raw_field))
            continue
        typed_field = field_name
        if typed_field in seen:
            continue
        seen.add(typed_field)
        normalized.append(typed_field)

    if unknown:
        raise ValueError(
            "Unknown execution policy field(s): "
            f"{', '.join(sorted(unknown))}. Expected one of {list(EXECUTION_POLICY_FIELDS)}."
        )

    return tuple(normalized)


class RuntimeOverrides(BaseModel):
    """Fields that can be set at any config layer."""

    model_config = ConfigDict(frozen=True)

    model: str | None = None
    harness: str | None = None
    agent: str | None = None
    effort: EffortValue = None
    sandbox: str | None = None
    approval: ApprovalValue = None
    autocompact: AutocompactValue = None
    autocompact_pct: AutocompactPctValue = None
    timeout: float | None = None

    def routing_scope(self) -> RuntimeOverrides:
        """Return only routing fields used to select model, harness, and agent."""

        return type(self).model_validate(
            {
                field_name: value
                for field_name in _ROUTING_OVERRIDE_FIELDS
                if (value := getattr(self, field_name)) is not None
            }
        )

    def execution_policy_scope(
        self,
        allowed_fields: frozenset[str] | None = None,
    ) -> RuntimeOverrides:
        """Return only execution-policy fields resolved after routing selection."""

        field_names = (
            _EXECUTION_POLICY_FIELD_SET
            if allowed_fields is None
            else frozenset(normalize_execution_policy_fields(allowed_fields))
        )
        return type(self).model_validate(
            {
                field_name: value
                for field_name in field_names
                if (value := getattr(self, field_name)) is not None
            }
        )

    def model_policy_scope(self) -> RuntimeOverrides:
        """Compatibility wrapper for execution-policy-only model policy precedence."""

        return self.execution_policy_scope()

    def to_env(self) -> dict[str, str]:
        """Render captured runtime overrides back to MERIDIAN_* env vars."""

        rendered: dict[str, str] = {}
        for field_name, env_name in RUNTIME_OVERRIDE_ENV_BY_FIELD.items():
            value = getattr(self, field_name)
            if value is None:
                continue
            rendered[env_name] = str(value)
        return rendered

    @field_validator("timeout")
    @classmethod
    def _validate_positive_float(cls, value: float | None, info: object) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or value <= 0:
            field_name = getattr(info, "field_name", "value")
            raise ValueError(
                f"Invalid runtime override '{field_name}': expected float > 0, got {value!r}."
            )
        return value

    @classmethod
    def from_env(cls) -> RuntimeOverrides:
        autocompact_raw = _read_env_string("MERIDIAN_AUTOCOMPACT")
        autocompact_pct_raw = _read_env_string("MERIDIAN_AUTOCOMPACT_PCT")
        timeout_raw = _read_env_string("MERIDIAN_TIMEOUT")
        return cls(
            model=_read_env_string("MERIDIAN_MODEL"),
            harness=None,  # MERIDIAN_HARNESS is spawn-local, not a policy override
            agent=_read_env_string("MERIDIAN_AGENT"),
            effort=_read_env_string("MERIDIAN_EFFORT"),
            sandbox=_read_env_string("MERIDIAN_SANDBOX"),
            approval=_read_env_string("MERIDIAN_APPROVAL"),
            autocompact=(
                _parse_env_int(autocompact_raw, env_name="MERIDIAN_AUTOCOMPACT")
                if autocompact_raw is not None
                else None
            ),
            autocompact_pct=(
                _parse_env_int(autocompact_pct_raw, env_name="MERIDIAN_AUTOCOMPACT_PCT")
                if autocompact_pct_raw is not None
                else None
            ),
            timeout=(
                _parse_env_float(timeout_raw, env_name="MERIDIAN_TIMEOUT")
                if timeout_raw is not None
                else None
            ),
        )

    @classmethod
    def from_agent_profile(cls, profile: AgentProfile | None) -> RuntimeOverrides:
        if profile is None:
            return cls()
        return cls(
            model=_normalize_optional_string(profile.model),
            harness=_normalize_optional_string(profile.harness),
            # Profile selection is the agent; it does not override itself.
            effort=_normalize_optional_string(profile.effort),
            sandbox=_normalize_optional_string(profile.sandbox),
            approval=_normalize_optional_string(profile.approval),
            autocompact=profile.autocompact,
            autocompact_pct=getattr(profile, "autocompact_pct", None),
        )

    @classmethod
    def from_alias_entry(cls, alias_entry: AliasEntry | None) -> RuntimeOverrides:
        if alias_entry is None:
            return cls()
        normalized_effort = _normalize_optional_string(alias_entry.default_effort)
        if normalized_effort == "auto":
            normalized_effort = None
        return cls(
            effort=normalized_effort,
            autocompact=alias_entry.default_autocompact,
            autocompact_pct=getattr(alias_entry, "default_autocompact_pct", None),
        )

    @classmethod
    def from_config(cls, config: MeridianConfig | None) -> RuntimeOverrides:
        if config is None:
            return cls()
        primary = config.primary
        return cls(
            model=primary.model,
            harness=primary.harness,
            agent=primary.agent,
            effort=primary.effort,
            sandbox=primary.sandbox,
            approval=primary.approval,
            autocompact=primary.autocompact,
            autocompact_pct=primary.autocompact_pct,
            timeout=primary.timeout,
        )

    @classmethod
    def from_spawn_config(cls, config: MeridianConfig | None) -> RuntimeOverrides:
        """Build overrides from spawn-level config defaults.

        Unlike from_config() which reads config.primary.*, this reads
        spawn defaults from config.default_model.

        Harness is intentionally excluded here so model->harness derivation
        can run when only a model is configured.
        """

        if config is None:
            return cls()
        return cls(
            model=_normalize_optional_string(config.default_model) or None,
        )

    @classmethod
    def from_agent_overlay_routing(cls, overlay: AgentOverlayConfig | None) -> RuntimeOverrides:
        """Extract routing fields (model, harness) from an agent overlay."""

        if overlay is None:
            return cls()
        return cls(
            model=overlay.model,
            harness=overlay.harness,
        )

    @classmethod
    def from_agent_overlay_policy(cls, overlay: AgentOverlayConfig | None) -> RuntimeOverrides:
        """Extract policy fields from an agent overlay."""

        if overlay is None:
            return cls()
        return cls(
            effort=overlay.effort,
            approval=overlay.approval,
            sandbox=overlay.sandbox,
            autocompact=overlay.autocompact,
            autocompact_pct=overlay.autocompact_pct,
        )

    @classmethod
    def from_spawn_input(cls, payload: SpawnCreateInput) -> RuntimeOverrides:
        return cls(
            model=_normalize_optional_string(payload.model),
            harness=_normalize_optional_string(payload.harness),
            agent=_normalize_optional_string(payload.agent),
            effort=_normalize_optional_string(payload.effort),
            sandbox=_normalize_optional_string(payload.sandbox),
            approval=_normalize_optional_string(payload.approval),
            autocompact=payload.autocompact,
            autocompact_pct=payload.autocompact_pct,
            timeout=payload.timeout,
        )


def resolve(*layers: RuntimeOverrides) -> RuntimeOverrides:
    """Merge layers with first-non-none precedence."""

    resolved: dict[str, object] = {}
    for field_name in RuntimeOverrides.model_fields:
        for layer in layers:
            value = getattr(layer, field_name)
            if value is not None:
                resolved[field_name] = value
                break
    return RuntimeOverrides.model_validate(resolved)


__all__ = [
    "EXECUTION_POLICY_FIELDS",
    "KNOWN_APPROVAL_VALUES",
    "KNOWN_EFFORT_VALUES",
    "ApprovalValue",
    "AutocompactPctValue",
    "AutocompactValue",
    "EffortValue",
    "ExecutionPolicyField",
    "RuntimeOverrides",
    "_validate_approval_value",
    "_validate_effort_value",
    "normalize_execution_policy_fields",
    "resolve",
]
