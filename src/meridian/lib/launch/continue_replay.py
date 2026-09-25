"""Canonical exact-continue replay contract for primary and spawn paths."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID

from meridian.lib.catalog.model_aliases import run_mars_models_resolve
from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.types import HarnessSessionId
from meridian.lib.harness.model_observation import (
    NativeModelReadContext,
    read_last_executed_model,
)
from meridian.lib.launch.policy_snapshot import managed_model_override_from_persisted_model
from meridian.lib.launch.request import SessionRequest
from meridian.lib.state.event_store import utc_now_iso
from meridian.lib.state.session_store import (
    ConversationModelSelection,
    SessionModelObservationEvent,
    SessionModelSelectionEvent,
    get_initial_model_selection,
    get_last_executed_model,
    get_model_selection,
    record_model_observation,
)

MODEL_OVERRIDE_WARNING = (
    "Continuing with an explicit model override. Resumed context may need processing "
    "without cached input, making the first response slower and potentially much more "
    "expensive for a long conversation, or consuming more of your plan allowance."
)


def _present(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


@dataclass(frozen=True)
class ContinueReplaySource:
    """Resolved source-session data needed to launch an exact continue."""

    source_ref: str
    harness_session_id: str | None
    harness: str | None
    source_chat_id: str | None
    source_work_id: str | None
    source_execution_cwd: str | None
    source_control_root: str | None
    source_claude_config_dir: str | None
    source_pi_session_dir: str | None
    source_launch_policy_snapshot: LaunchPolicySnapshot | None
    tracked: bool
    source_native_store: str | None = None
    source_history_id: UUID | None = None
    source_model: str | None = None
    source_agent: str | None = None
    source_skills: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContinueReplayContract:
    """Launch-ready exact-continue replay contract."""

    model: str | None
    agent: str | None
    agent_opt_out: bool
    skills: tuple[str, ...]
    harness: str
    passthrough_args: tuple[str, ...]
    launch_policy_snapshot: LaunchPolicySnapshot | None
    session: SessionRequest
    work_id: str | None
    task_dir: str | None


class ContinueReplayReference(Protocol):
    """Resolved reference shape needed to build a continue replay source.

    The protocol is expressed in launch-owned terms. Recovery choices, such as
    whether a recovered harness session id is authoritative enough to use, stay
    in the caller; the caller passes the final harness_session_id explicitly.
    """

    @property
    def harness(self) -> str | None: ...

    @property
    def source_chat_id(self) -> str | None: ...

    @property
    def source_history_id(self) -> UUID | None: ...

    @property
    def source_model(self) -> str | None: ...

    @property
    def source_agent(self) -> str | None: ...

    @property
    def source_skills(self) -> tuple[str, ...]: ...

    @property
    def source_work_id(self) -> str | None: ...

    @property
    def source_execution_cwd(self) -> str | None: ...

    @property
    def source_control_root(self) -> str | None: ...

    @property
    def source_claude_config_dir(self) -> str | None: ...

    @property
    def source_pi_session_dir(self) -> str | None: ...

    @property
    def source_native_store(self) -> str | None: ...

    @property
    def source_launch_policy_snapshot(self) -> LaunchPolicySnapshot | None: ...

    @property
    def tracked(self) -> bool: ...


def continue_replay_source_from_reference(
    source_ref: str,
    resolved_reference: ContinueReplayReference,
    *,
    harness_session_id: str | None,
) -> ContinueReplaySource:
    """Build continue replay source inputs from a resolved session reference."""

    return ContinueReplaySource(
        source_ref=source_ref,
        harness_session_id=harness_session_id,
        harness=resolved_reference.harness,
        source_chat_id=resolved_reference.source_chat_id,
        source_history_id=resolved_reference.source_history_id,
        source_model=resolved_reference.source_model,
        source_agent=resolved_reference.source_agent,
        source_skills=resolved_reference.source_skills,
        source_work_id=resolved_reference.source_work_id,
        source_execution_cwd=resolved_reference.source_execution_cwd,
        source_control_root=resolved_reference.source_control_root,
        source_claude_config_dir=resolved_reference.source_claude_config_dir,
        source_pi_session_dir=resolved_reference.source_pi_session_dir,
        source_native_store=resolved_reference.source_native_store,
        source_launch_policy_snapshot=resolved_reference.source_launch_policy_snapshot,
        tracked=resolved_reference.tracked,
    )


def _resolve_replay_harness(
    *,
    source: ContinueReplaySource,
    explicit_harness: str | None,
) -> str:
    explicit = _present(explicit_harness)
    source_harness = _present(source.harness)
    snapshot_harness = (
        _present(source.source_launch_policy_snapshot.harness)
        if source.source_launch_policy_snapshot is not None
        else None
    )
    named = tuple(
        (name, value)
        for name, value in (
            ("explicit", explicit),
            ("source", source_harness),
            ("snapshot", snapshot_harness),
        )
        if value is not None
    )
    unique_values = {value for _, value in named}
    if len(unique_values) > 1:
        details = ", ".join(f"{name} is '{value}'" for name, value in named)
        flag_hint = " --harness" if explicit is not None else ""
        raise ValueError(
            f"Cannot continue across harnesses{flag_hint}: {details}. "
            "Use --fork-fresh to change launch identity."
        )
    if named:
        return named[0][1]
    raise ValueError(
        f"Session '{source.harness_session_id or source.source_ref}' "
        "not recognized by any harness. "
        "Use --harness to specify which harness owns this session."
    )


def _reject_exact_continue_agent_override(
    *,
    requested_agent: str | None,
    agent_opt_out: bool,
) -> None:
    if _present(requested_agent) is not None:
        raise ValueError(
            "Cannot combine exact continue with --agent. "
            "Use --fork-fresh to change launch identity."
        )
    if agent_opt_out:
        raise ValueError(
            "Cannot combine exact continue with agent opt-out (--agent ''). "
            "Use --fork-fresh to change launch identity."
        )


def _fallback_conversation_intent(
    *,
    source: ContinueReplaySource,
    snapshot_model: str | None,
) -> ConversationModelSelection:
    snapshot = source.source_launch_policy_snapshot
    if snapshot is None:
        return ConversationModelSelection(
            requested_token=snapshot_model,
            selection_source="initial_launch",
        )
    canonical = snapshot.model_selection_canonical_id
    model = canonical or snapshot_model
    if model is None:
        return ConversationModelSelection(
            requested_token=None,
            selected_token=snapshot.model_selection_selected_token,
            model_mode="harness_default",
            selection_source="initial_launch",
        )
    return ConversationModelSelection(
        requested_token=model,
        selected_token=snapshot.model_selection_selected_token,
        canonical_model_id=canonical,
        provider_constraint=snapshot.model_selection_provider_constraint,
        selection_source="initial_launch",
    )


def _payload_harness(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    harness = cast("dict[str, object]", value).get("harness")
    return harness if isinstance(harness, str) else None


def _observed_model_routes_to_harness(token: str, harness: str) -> bool:
    """Best-effort check that an observed token can launch on the replay harness.

    ``mars models resolve`` can return a payload for a token that does not route
    to the harness being continued — for example an OpenCode session on an
    unconfigured provider (``opencode/deepseek-v4-flash-free``), or a token whose
    only candidates lack a runnable harness (``claude-fable-5``). Launch pins
    ``--harness`` to the source session's harness, so a token must route to that
    same harness; otherwise preferring it would make ``--continue`` fail where
    the recorded startup selection worked.
    """

    resolved: dict[str, object] | None = None
    with contextlib.suppress(Exception):
        resolved = run_mars_models_resolve(token)
    if resolved is None or resolved.get("error"):
        return False
    if _payload_harness(resolved.get("route")) == harness or resolved.get("harness") == harness:
        return True
    runnable_paths = resolved.get("runnable_paths")
    if not isinstance(runnable_paths, list):
        return False
    return any(
        _payload_harness(path) == harness for path in cast("list[object]", runnable_paths)
    )


def _observed_last_executed_model(
    *,
    source: ContinueReplaySource,
    replay_harness: str,
    runtime_root: Path,
) -> str | None:
    """Resolve the last-executed model, live-read first with stored observation fallback.

    Live-read hits are best-effort persisted as an observation; a persistence
    failure must never break continue, so it is swallowed here. Unroutable
    observations are discarded so the caller falls back to the recorded selection.
    """

    harness_session_id = source.harness_session_id
    if harness_session_id is None:
        return None
    live = read_last_executed_model(
        replay_harness,
        harness_session_id,
        context=NativeModelReadContext(
            project_root=source.source_control_root,
            claude_config_dir=source.source_claude_config_dir,
            pi_session_dir=source.source_pi_session_dir,
        ),
    )
    token = live if live is not None else get_last_executed_model(
        runtime_root, replay_harness, harness_session_id
    )
    if token is None or not _observed_model_routes_to_harness(token, replay_harness):
        return None
    if live is not None:
        with contextlib.suppress(Exception):
            record_model_observation(
                runtime_root,
                SessionModelObservationEvent(
                    harness=replay_harness,
                    harness_session_id=HarnessSessionId(harness_session_id),
                    observed_model_token=live,
                    recorded_at=utc_now_iso(),
                ),
            )
    return token


def _resolve_continue_conversation_intent(
    *,
    source: ContinueReplaySource,
    replay_harness: str,
    fork: bool,
    requested_model_override: str | None,
    runtime_root: Path | None,
    snapshot_model: str | None,
) -> tuple[ConversationModelSelection, SessionModelSelectionEvent | None]:
    recorded = (
        get_model_selection(runtime_root, replay_harness, source.harness_session_id)
        if runtime_root is not None and source.harness_session_id is not None and not fork
        else None
    )
    seed_event = (
        get_initial_model_selection(
            runtime_root, replay_harness, source.harness_session_id,
            source_chat_id=source.source_chat_id,
        )
        if recorded is None and runtime_root is not None
        and source.harness_session_id is not None and not fork
        else None
    )
    if requested_model_override is not None:
        return (
            ConversationModelSelection(
                requested_token=requested_model_override,
                selection_source="explicit_override",
            ),
            seed_event,
        )
    if not fork and runtime_root is not None and source.harness_session_id is not None:
        observed_token = _observed_last_executed_model(
            source=source,
            replay_harness=replay_harness,
            runtime_root=runtime_root,
        )
        if observed_token is not None:
            return (
                ConversationModelSelection(
                    requested_token=observed_token,
                    selection_source="observed_last_used",
                ),
                seed_event,
            )
    if recorded is not None:
        return recorded.model_copy(update={"selection_source": "recorded_selection"}), None
    if seed_event is not None:
        selection = seed_event.selection
        routing = selection.canonical_model_id or selection.selected_token
        return (
            selection.model_copy(update={"requested_token": routing or selection.requested_token}),
            seed_event,
        )
    if source.tracked and runtime_root is not None and not fork:
        raise ValueError(
            "No accepted model selection or original session history is recorded. "
            "Continue with an explicit --model."
        )
    return _fallback_conversation_intent(source=source, snapshot_model=snapshot_model), None


def build_continue_replay_contract(
    *,
    source: ContinueReplaySource,
    explicit_harness: str | None = None,
    requested_agent: str | None = None,
    agent_opt_out: bool = False,
    fork: bool = False,
    requested_model_override: str | None = None,
    runtime_root: Path | None = None,
) -> ContinueReplayContract:
    """Build the normalized exact-continue contract from a resolved source."""

    _reject_exact_continue_agent_override(
        requested_agent=requested_agent,
        agent_opt_out=agent_opt_out,
    )
    replay_harness = _resolve_replay_harness(
        source=source,
        explicit_harness=explicit_harness,
    )

    snapshot = source.source_launch_policy_snapshot
    if snapshot is not None:
        snapshot_model = managed_model_override_from_persisted_model(snapshot.model)
        agent = _present(snapshot.agent)
        replay_agent_opt_out = snapshot.agent_opt_out
        skills = snapshot.skills
        passthrough_args = snapshot.extra_args
    else:
        snapshot_model = _present(source.source_model)
        agent = None
        replay_agent_opt_out = False
        skills = ()
        passthrough_args = ()

    intent, seed_event = _resolve_continue_conversation_intent(
        source=source,
        replay_harness=replay_harness,
        fork=fork,
        requested_model_override=requested_model_override,
        runtime_root=runtime_root,
        snapshot_model=snapshot_model,
    )

    session = SessionRequest(
        requested_harness_session_id=source.harness_session_id,
        initial_model_selection=seed_event,
        conversation_intent=intent,
        continue_harness=replay_harness,
        continue_source_tracked=source.tracked,
        continue_source_ref=source.source_ref,
        continue_fork=fork,
        continue_chat_id=source.source_chat_id,
        forked_from_chat_id=source.source_chat_id if fork else None,
        forked_from_history_id=source.source_history_id if fork else None,
        source_control_root=source.source_control_root,
        source_execution_cwd=source.source_execution_cwd,
        source_claude_config_dir=source.source_claude_config_dir,
        source_pi_session_dir=source.source_pi_session_dir,
        source_native_store=source.source_native_store,
    )

    return ContinueReplayContract(
        model=intent.routing_token,
        agent=agent,
        agent_opt_out=replay_agent_opt_out,
        skills=skills,
        harness=replay_harness,
        passthrough_args=passthrough_args,
        launch_policy_snapshot=snapshot,
        session=session,
        work_id=source.source_work_id,
        task_dir=source.source_execution_cwd,
    )


__all__ = [
    "ContinueReplayContract",
    "ContinueReplaySource",
    "build_continue_replay_contract",
    "continue_replay_source_from_reference",
]
