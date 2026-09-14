"""Neutral launch types shared between harness adapters and launch orchestration.

Placed in ``meridian.lib.harness`` (not ``meridian.lib.space``) to avoid
import-cycle pressure from ``space/__init__.py`` which eagerly imports
``space.launch``.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class SessionSeed(BaseModel):
    """Adapter's session decisions, resolved early (before process starts)."""

    model_config = ConfigDict(frozen=True)

    session_id: str = ""
    session_args: tuple[str, ...] = ()


class ManagedPrimaryPreview(BaseModel):
    """Non-executing description of the actual managed launch stages."""

    model_config = ConfigDict(frozen=True)
    backend_command: tuple[str, ...]
    bootstrap_method: Literal["GET", "POST"]
    bootstrap_path: str
    bootstrap_payload: dict[str, object]
    attach_command: tuple[str, ...]
    steps: tuple[str, ...]
    model: str | None = None
    requested_model: str | None = None
    native_observations: Literal["unavailable (dry-run)"] = "unavailable (dry-run)"
