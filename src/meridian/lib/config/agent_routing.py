"""Routing for an agent Meridian runs on its own behalf, set in the user's ``config.toml``.

The section is user-only (``user_only.py``): the agent reads text written by strangers, so a
file a sandboxed session can write must not pick its model or harness.
"""

from __future__ import annotations

from typing import Final

from pydantic import ConfigDict, ValidationError, field_validator

from meridian.lib.config.errors import config_validation_error, warn_unknown_model_keys
from meridian.lib.config.model_base import ConfigModel
from meridian.lib.config.user_only import read_user_only_section, user_config_path
from meridian.lib.core.overrides import EffortValue, RuntimeOverrides
from meridian.lib.core.types import HarnessId
from meridian.lib.utils.json_shapes import is_json_object

# Agent name -> (user-only section, its table). The section must be in ``USER_ONLY_SECTIONS``.
AGENT_ROUTING_SECTIONS: Final[dict[str, tuple[str, str]]] = {
    "inbox-router": ("inbox", "router"),
}


class _AgentRouting(ConfigModel):
    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    model: str | None = None
    harness: str | None = None
    effort: EffortValue = None

    @field_validator("model", mode="before")
    @classmethod
    def _validate_model(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        if not value.strip():
            raise ValueError("expected non-empty string")
        return value.strip()

    @field_validator("harness", mode="before")
    @classmethod
    def _validate_harness(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        launched = [harness.value for harness in HarnessId if harness.launched_by_meridian]
        if value.strip() not in launched:
            raise ValueError(f"expected one of {', '.join(launched)}, got {value!r}")
        return value.strip()


def configured_agent_routing(agent: str | None) -> RuntimeOverrides:
    """The model, harness and effort the user set for *agent*; empty when it has no section.

    Raises ``ValueError`` naming the file and key when the section is malformed.
    """

    location = AGENT_ROUTING_SECTIONS.get(agent or "")
    if location is None:
        return RuntimeOverrides()
    section, table = location
    source = f"{section}.{table}"
    path = user_config_path()
    raw_section = read_user_only_section(section)
    if raw_section is None:
        return RuntimeOverrides()
    if not is_json_object(raw_section):
        raise ValueError(f"Invalid value for '{section}' in '{path}': expected table.")
    raw = raw_section.get(table)
    if raw is None:
        return RuntimeOverrides()
    warn_unknown_model_keys(raw, model=_AgentRouting, source=source)
    try:
        routing = _AgentRouting.model_validate(raw)
    except ValidationError as error:
        message = str(config_validation_error(error, source=source)).removesuffix(".")
        raise ValueError(f"{message} in '{path}'.") from None
    return RuntimeOverrides.model_validate(routing.model_dump())


__all__ = ["AGENT_ROUTING_SECTIONS", "configured_agent_routing"]
