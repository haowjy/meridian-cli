"""Leaf launch contracts shared across harness adapters and runners."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, cast, runtime_checkable

from pydantic import BaseModel, ConfigDict, model_validator

from meridian.lib.core.overrides import (
    ExecutionPolicyField,
    RuntimeOverrides,
    normalize_execution_policy_fields,
)
from meridian.lib.harness.ids import HarnessId

if TYPE_CHECKING:
    from meridian.lib.safety.permissions import PermissionConfig


class CompositionWarning(BaseModel):
    """User-visible warning emitted by launch composition stages."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    code: str
    message: str
    detail: dict[str, str] | None = None

    @model_validator(mode="before")
    @classmethod
    def _freeze_detail(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        raw_value = cast("dict[Any, Any]", value)
        payload: dict[str, Any] = {str(key): item for key, item in raw_value.items()}
        detail = payload.get("detail")
        if isinstance(detail, dict):
            raw_detail = cast("dict[Any, Any]", detail)
            payload["detail"] = {str(key): str(item) for key, item in raw_detail.items()}
        return payload


@runtime_checkable
class PermissionResolver(Protocol):
    """Transport-neutral permission intent resolver."""

    @property
    def config(self) -> PermissionConfig: ...

    def resolve_flags(self) -> tuple[str, ...]:
        return ()


class ResolvedLaunchSpec(BaseModel):
    """Transport-neutral resolved configuration for one harness launch."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    # Identity
    model: str | None = None

    # Execution parameters
    effort: str | None = None
    prompt: str = ""

    # Session continuity
    continue_session_id: str | None = None
    continue_fork: bool = False

    # Permissions
    permission_resolver: PermissionResolver

    # Passthrough args forwarded verbatim to the harness process.
    extra_args: tuple[str, ...] = ()

    # Interactive mode
    interactive: bool = False

    # Harness-agnostic MCP tool configuration.
    mcp_tools: tuple[str, ...] = ()

    # First-class workspace root projection — harness-agnostic, deduplicated.
    projected_roots: tuple[Path, ...] = ()

    @model_validator(mode="after")
    def _validate_continue_fork_requires_session(self) -> ResolvedLaunchSpec:
        if self.continue_fork and not self.continue_session_id:
            raise ValueError("continue_fork=True requires continue_session_id")
        return self


@dataclass(frozen=True)
class ResolvedLaunchRouting:
    """Typed launch-routing output from policy resolution."""

    model: str | None
    harness: HarnessId
    agent: str | None

    def as_overrides(self) -> RuntimeOverrides:
        """Compatibility view for legacy RuntimeOverrides consumers."""

        return RuntimeOverrides(
            model=self.model,
            harness=self.harness.value,
            agent=self.agent,
        )


@dataclass(frozen=True)
class ResolvedExecutionPolicy:
    """Typed execution-policy output from policy resolution."""

    effort: str | None = None
    sandbox: str | None = None
    approval: str | None = None
    autocompact: int | None = None
    timeout: float | None = None

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
            "timeout": self.timeout,
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


SpecT = TypeVar("SpecT", bound=ResolvedLaunchSpec)


def summarize_composition_warnings(
    warnings: tuple[CompositionWarning, ...],
) -> str | None:
    parts = [warning.message.strip() for warning in warnings if warning.message.strip()]
    if not parts:
        return None
    return "; ".join(parts)


@dataclass(frozen=True)
class PreflightResult:
    """Result of adapter-owned preflight."""

    expanded_passthrough_args: tuple[str, ...]
    extra_env: MappingProxyType[str, str]

    @classmethod
    def build(
        cls,
        *,
        expanded_passthrough_args: tuple[str, ...],
        extra_env: dict[str, str] | None = None,
    ) -> PreflightResult:
        return cls(
            expanded_passthrough_args=expanded_passthrough_args,
            extra_env=MappingProxyType(dict(extra_env or {})),
        )
