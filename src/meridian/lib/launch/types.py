"""Shared types for the launch pipeline."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from meridian.lib.core.execution_policy import ResolvedExecutionPolicy
from meridian.lib.launch.composition import PromptDocument
from meridian.lib.launch.request import SessionRequest

_CONTINUATION_GUIDANCE = (
    "You are resuming an existing Meridian session. Continue from the current state, "
    "preserve prior decisions unless evidence has changed, and avoid duplicating "
    "already-completed work."
)
_FORK_GUIDANCE = (
    "You are working in a forked Meridian session - a branch from a prior conversation. "
    "You have the full context from the original session. The user wants to explore "
    "a different direction from here. Do not repeat completed work."
)


class SessionMode(StrEnum):
    """How this launch relates to prior conversation state."""

    FRESH = "fresh"
    RESUME = "resume"
    FORK = "fork"


@dataclass(frozen=True)
class SessionIntent:
    """Resolved session intent for launch planning."""

    mode: SessionMode
    harness_session_id: str | None = None
    chat_id: str | None = None
    forked_from_chat_id: str | None = None


class LaunchRequest(BaseModel):
    """Inputs for launching one primary agent session."""

    model_config = ConfigDict(frozen=True)

    model: str = ""
    harness: str | None = None
    agent: str | None = None
    work_id: str | None = None
    # Deprecated: use `session_mode` for new code.
    fresh: bool = True
    session_mode: SessionMode = SessionMode.FRESH
    passthrough_args: tuple[str, ...] = ()
    pinned_context: str = ""
    supplemental_prompt_documents: tuple[PromptDocument, ...] = ()
    include_bootstrap_documents: bool = False
    dry_run: bool = False
    # Execution policy carrier (replaces flat effort/sandbox/approval/autocompact/etc.)
    execution_policy: ResolvedExecutionPolicy = Field(default_factory=ResolvedExecutionPolicy)
    session: SessionRequest = Field(default_factory=SessionRequest)

    @model_validator(mode="before")
    @classmethod
    def _migrate_flat_policy_fields(cls, value: object) -> object:
        """Migrate legacy flat policy fields to the execution_policy carrier."""
        if not isinstance(value, dict):
            return value
        payload = cast("dict[str, Any]", value).copy()
        if "execution_policy" in payload:
            return payload
        policy_fields: dict[str, Any] = {}
        for field_name in ("effort", "sandbox", "autocompact", "autocompact_pct", "timeout"):
            if field_name in payload:
                policy_fields[field_name] = payload.pop(field_name)
        # approval="default" means "no explicit override" — treat as None in carrier
        if "approval" in payload:
            approval_val = payload.pop("approval")
            if approval_val != "default":
                policy_fields["approval"] = approval_val
        if policy_fields:
            payload["execution_policy"] = policy_fields
        return payload

    @model_validator(mode="before")
    @classmethod
    def _sync_session_mode_fields(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value

        payload = cast("dict[str, Any]", value).copy()
        has_session_mode = payload.get("session_mode") is not None
        has_fresh = payload.get("fresh") is not None

        if has_session_mode:
            mode_raw = payload["session_mode"]
            mode = mode_raw if isinstance(mode_raw, SessionMode) else SessionMode(str(mode_raw))
            payload["fresh"] = mode == SessionMode.FRESH
            return payload

        if has_fresh:
            payload["session_mode"] = (
                SessionMode.FRESH if bool(payload["fresh"]) else SessionMode.RESUME
            )
            return payload

        return payload


class LaunchResult(BaseModel):
    """Result metadata from a completed primary launch."""

    model_config = ConfigDict(frozen=True)

    command: tuple[str, ...]
    exit_code: int
    continue_ref: str | None = None
    continue_chat_id: str | None = None
    warning: str | None = None
    terminal_surface_mode: str | None = None


class PrimarySessionMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    harness: str
    model: str
    agent: str
    agent_path: str
    skills: tuple[str, ...]
    skill_paths: tuple[str, ...]


def build_primary_prompt(request: LaunchRequest) -> str:
    """Build launch prompt for primary sessions."""

    sections: list[str] = ["# Meridian Session"]

    if request.session_mode == SessionMode.FRESH:
        sections.extend(
            [
                "",
                "# Session Mode",
                "",
                "Start a fresh primary conversation for this space.",
            ]
        )
    elif request.session_mode == SessionMode.FORK:
        sections.extend(["", "# Fork Guidance", "", _FORK_GUIDANCE])
    else:
        sections.extend(["", "# Continuation Guidance", "", _CONTINUATION_GUIDANCE])

    if request.pinned_context.strip():
        sections.extend(["", "# Re-Injected Pinned Context", "", request.pinned_context.strip()])

    return "\n".join(sections).strip()


__all__ = [
    "LaunchRequest",
    "LaunchResult",
    "PrimarySessionMetadata",
    "SessionIntent",
    "SessionMode",
    "build_primary_prompt",
]
