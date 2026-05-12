"""Execution policy carrier — the opaque bundle of post-routing launch controls."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.overrides import (
    AutocompactPctValue,
    AutocompactValue,
    ExecutionPolicyField,
    RuntimeOverrides,
    normalize_execution_policy_fields,
)


class ResolvedExecutionPolicy(BaseModel):
    """Typed execution-policy carrier passed through non-consuming boundaries.

    Only the compiler (precedence resolution) and harness adapters (env/flag
    injection) should access individual fields. Everything else carries this
    as an opaque object.
    """

    model_config = ConfigDict(frozen=True)

    effort: str | None = None
    sandbox: str | None = None
    approval: str | None = None
    autocompact: AutocompactValue = None
    autocompact_pct: AutocompactPctValue = None
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
            "autocompact_pct": self.autocompact_pct,
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


__all__ = ["ResolvedExecutionPolicy"]
