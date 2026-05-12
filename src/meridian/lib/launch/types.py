"""Shared types for the launch pipeline."""

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

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
    session_mode: SessionMode = SessionMode.FRESH
    passthrough_args: tuple[str, ...] = ()
    pinned_context: str = ""
    supplemental_prompt_documents: tuple[PromptDocument, ...] = ()
    include_bootstrap_documents: bool = False
    dry_run: bool = False
    # Execution policy carrier (replaces flat effort/sandbox/approval/autocompact/etc.)
    execution_policy: ResolvedExecutionPolicy = Field(default_factory=ResolvedExecutionPolicy)
    session: SessionRequest = Field(default_factory=SessionRequest)


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
