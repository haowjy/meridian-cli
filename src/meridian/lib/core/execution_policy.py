"""Execution policy carrier — the opaque bundle of post-routing launch controls."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, cast

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    PlainSerializer,
    TypeAdapter,
    field_serializer,
    field_validator,
)

from meridian.lib.core.overrides import (
    AutocompactPctValue,
    AutocompactValue,
    ExecutionPolicyField,
    RuntimeOverrides,
    normalize_execution_policy_fields,
)
from meridian.lib.safety.policy import (
    NetworkPolicy,
    network_policy_to_wire,
    parse_network_policy,
)

type NativeConfig = Mapping[str, object]

_NATIVE_CONFIG_ADAPTER = TypeAdapter(dict[str, JsonValue])


def _empty_native_config() -> NativeConfig:
    return MappingProxyType({})


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        mapping = cast("Mapping[object, object]", value)
        return MappingProxyType({str(key): _freeze_json(item) for key, item in mapping.items()})
    if isinstance(value, list | tuple):
        sequence = cast("list[object] | tuple[object, ...]", value)
        return tuple(_freeze_json(item) for item in sequence)
    return value


def _thaw_json(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        mapping = cast("Mapping[object, object]", value)
        return {str(key): _thaw_json(item) for key, item in mapping.items()}
    if isinstance(value, tuple):
        sequence = cast("tuple[object, ...]", value)
        return [_thaw_json(item) for item in sequence]
    return cast("JsonValue", value)


def validate_native_config(value: object) -> dict[str, JsonValue]:
    """Validate the bundle's optional JSON object before freezing it."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("expected a JSON object")
    return _NATIVE_CONFIG_ADAPTER.validate_python(
        _thaw_json(cast("Mapping[object, object]", value))
    )


def freeze_native_config(value: Mapping[str, object]) -> NativeConfig:
    """Recursively freeze a validated native config tree."""

    return cast("NativeConfig", _freeze_json(value))


def native_config_to_json(value: NativeConfig) -> dict[str, JsonValue]:
    """Return an ordinary JSON tree for persistence or wire serialization."""

    return cast("dict[str, JsonValue]", _thaw_json(value))


NativeConfigValue = Annotated[
    NativeConfig,
    BeforeValidator(validate_native_config),
    AfterValidator(freeze_native_config),
    PlainSerializer(native_config_to_json, return_type=dict[str, JsonValue]),
]


class ResolvedExecutionPolicy(BaseModel):
    """Typed execution-policy carrier passed through non-consuming boundaries.

    Only the compiler (precedence resolution) and harness adapters (env/flag
    injection) should access individual fields. Everything else carries this
    as an opaque object.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    effort: str | None = None
    sandbox: str | None = None
    network: NetworkPolicy | None = None
    approval: str | None = None
    autocompact: AutocompactValue = None
    autocompact_pct: AutocompactPctValue = None
    timeout: float | None = None
    resident_rearm_budget: int | None = None
    native_config: NativeConfigValue = Field(default_factory=_empty_native_config)

    @field_validator("network", mode="before")
    @classmethod
    def _parse_network(cls, value: object) -> NetworkPolicy | None:
        # Scalar strings are the durable shape written before typed host policies.
        return parse_network_policy(value)

    @field_serializer("network")
    def _serialize_network(self, value: NetworkPolicy | None) -> dict[str, object] | None:
        return network_policy_to_wire(value) if value is not None else None

    @field_validator("resident_rearm_budget")
    @classmethod
    def _validate_resident_rearm_budget(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or value < 0:
            raise ValueError(f"expected int >= 0, got {value!r}")
        return value

    def as_overrides(
        self,
        supported_fields: frozenset[ExecutionPolicyField] | None = None,
    ) -> RuntimeOverrides:
        """Compatibility view for legacy RuntimeOverrides consumers."""

        allowed_fields = (
            None
            if supported_fields is None
            else frozenset(normalize_execution_policy_fields(supported_fields))
        )
        values = {
            "effort": self.effort,
            "sandbox": self.sandbox,
            "approval": self.approval,
            "autocompact": self.autocompact,
            "autocompact_pct": self.autocompact_pct,
            "timeout": self.timeout,
            "resident_rearm_budget": self.resident_rearm_budget,
        }
        if allowed_fields is not None:
            values = {
                field_name: value
                for field_name, value in values.items()
                if field_name in allowed_fields
            }
        return RuntimeOverrides.model_validate(
            {field_name: value for field_name, value in values.items() if value is not None}
        )


__all__ = [
    "NativeConfig",
    "NativeConfigValue",
    "ResolvedExecutionPolicy",
    "freeze_native_config",
    "native_config_to_json",
    "validate_native_config",
]
