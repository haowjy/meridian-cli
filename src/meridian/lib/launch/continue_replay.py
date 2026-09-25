"""Pure continue intent selection and launch-contract assembly."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.launch.request import SessionRequest
from meridian.lib.state.session_authority import (
    ConversationModelSelection,
    ExactModelObservation,
    RecordedNativeSource,
    ReplayModelFacts,
    SessionModelSelectionEvent,
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
    recorded_native_source: RecordedNativeSource | None = None
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
    def source_launch_policy_snapshot(self) -> LaunchPolicySnapshot | None: ...

    @property
    def tracked(self) -> bool: ...


def continue_replay_source_from_reference(
    source_ref: str,
    resolved_reference: ContinueReplayReference,
    *,
    harness_session_id: str | None,
    recorded_native_source: RecordedNativeSource | None = None,
) -> ContinueReplaySource:
    """Build continue replay source inputs from a resolved session reference."""

    return ContinueReplaySource(
        source_ref=source_ref,
        recorded_native_source=recorded_native_source,
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


@dataclass(frozen=True, init=False)
class ContinueReplayIntent:
    """Detached value views: the selection's mutable provenance never escapes."""

    _selection: ConversationModelSelection
    _initial_model_selection: SessionModelSelectionEvent | None
    recorded_native_source: RecordedNativeSource | None

    def __init__(
        self,
        selection: ConversationModelSelection,
        initial_model_selection: SessionModelSelectionEvent | None = None,
        recorded_native_source: RecordedNativeSource | None = None,
    ) -> None:
        object.__setattr__(self, "_selection", selection.model_copy(deep=True))
        object.__setattr__(
            self,
            "_initial_model_selection",
            (
                initial_model_selection.model_copy(deep=True)
                if initial_model_selection is not None
                else None
            ),
        )
        object.__setattr__(self, "recorded_native_source", recorded_native_source)

    @property
    def selection(self) -> ConversationModelSelection:
        return self._selection.model_copy(deep=True)

    @property
    def initial_model_selection(self) -> SessionModelSelectionEvent | None:
        return (
            self._initial_model_selection.model_copy(deep=True)
            if self._initial_model_selection is not None
            else None
        )


@dataclass(frozen=True)
class ContinueReplayRefused:
    reason: Literal[
        "missing_intent",
        "mixed_evidence",
        "source_conflict",
        "metadata_mismatch",
        "unsupported_view",
        "unsupported_provider",
        "evidence_mismatch",
    ]
    message: str


@dataclass(frozen=True)
class EligibleContinueObservation:
    """Value-only policy input; no managed Pi producer qualifies this slot yet."""

    selection: ConversationModelSelection
    evidence: ExactModelObservation


def _accepted_selection(
    selection: ConversationModelSelection, *, invocation: bool, exact: bool
) -> ConversationModelSelection:
    selection = selection.model_copy(deep=True)
    if invocation:
        selection = selection.model_copy(update={"selection_source": "recorded_selection"})
    if exact and selection.model_mode == "harness_default":
        return selection.model_copy(update={"requested_token": None, "selected_token": None})
    if not invocation:
        return selection.model_copy(
            update={
                "requested_token": selection.canonical_model_id
                or selection.selected_token
                or selection.requested_token
            }
        )
    return selection


def select_continue_replay_intent(
    *,
    operation: Literal["resume", "fork"],
    requested_model_override: str | None = None,
    exact_facts: ReplayModelFacts | None = None,
    recorded_native_source: RecordedNativeSource | None = None,
    eligible_observation: EligibleContinueObservation | None = None,
    legacy_invocation: ConversationModelSelection | None = None,
    legacy_observation: ConversationModelSelection | None = None,
    recovered_seed: SessionModelSelectionEvent | None = None,
    fallback: ConversationModelSelection | None = None,
) -> ContinueReplayIntent | ContinueReplayRefused:
    """Select from collected values, never look up a source or resolve a token."""
    if (exact_facts is None) != (recorded_native_source is None):
        return ContinueReplayRefused(
            "source_conflict", "Exact replay requires retained facts and their recorded source."
        )
    if exact_facts is not None and (
        legacy_invocation is not None
        or legacy_observation is not None
        or recovered_seed is not None
        or fallback is not None
    ):
        return ContinueReplayRefused(
            "mixed_evidence", "Exact replay cannot consume legacy fallback values."
        )
    if exact_facts is not None:
        for fact in (exact_facts.latest_invocation, exact_facts.first_committed_seed):
            if fact is not None and (
                fact.correlation != "exact_source" or fact.value.source != recorded_native_source
            ):
                return ContinueReplayRefused(
                    "source_conflict", "Accepted intent belongs to a different source."
                )
    seed = recovered_seed if operation == "resume" else None
    if requested_model_override is not None:
        selection = ConversationModelSelection(
            requested_token=requested_model_override, selection_source="explicit_override"
        )
    elif operation == "fork":
        if fallback is None:
            return ContinueReplayRefused(
                "missing_intent", "Fork inheritance intent is unavailable."
            )
        selection = fallback
    elif eligible_observation is not None:
        evidence = eligible_observation.evidence
        if evidence.source != recorded_native_source:
            return ContinueReplayRefused(
                "source_conflict", "Observed intent belongs to a different source."
            )
        selection = eligible_observation.selection.model_copy(
            deep=True,
            update={
                "selection_source": "observed_last_used",
                "provenance": {
                    **eligible_observation.selection.provenance,
                    "model_basis": evidence.model_basis,
                    "provider_contract": evidence.provider_contract,
                    "view_basis": evidence.view_basis,
                    "native_provider": evidence.native_provider,
                    "native_model": evidence.model_token,
                },
            },
        )
    elif legacy_observation is not None:
        selection = legacy_observation
    elif exact_facts is not None and exact_facts.latest_invocation is not None:
        selection = _accepted_selection(
            exact_facts.latest_invocation.event.selection, invocation=True, exact=True
        )
    elif legacy_invocation is not None:
        selection = _accepted_selection(legacy_invocation, invocation=True, exact=False)
        seed = None
    elif exact_facts is not None and exact_facts.first_committed_seed is not None:
        selection = _accepted_selection(
            exact_facts.first_committed_seed.event.selection, invocation=False, exact=True
        )
    elif seed is not None:
        selection = _accepted_selection(seed.selection, invocation=False, exact=False)
    elif fallback is not None:
        selection = fallback
    else:
        return ContinueReplayRefused(
            "missing_intent",
            "No accepted model selection or original session history is recorded. "
            "Continue with an explicit --model.",
        )
    return ContinueReplayIntent(selection, seed, recorded_native_source)


def build_continue_replay_contract(
    *,
    source: ContinueReplaySource,
    intent: ContinueReplayIntent,
    explicit_harness: str | None = None,
    requested_agent: str | None = None,
    agent_opt_out: bool = False,
    fork: bool = False,
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
        snapshot = snapshot.model_copy(deep=True)
        agent = _present(snapshot.agent)
        replay_agent_opt_out = snapshot.agent_opt_out
        skills = snapshot.skills
        passthrough_args = snapshot.extra_args
    else:
        agent = None
        replay_agent_opt_out = False
        skills = ()
        passthrough_args = ()

    recorded_source = source.recorded_native_source
    if intent.recorded_native_source != recorded_source:
        raise ValueError("Continue intent and replay source identities differ")
    if recorded_source is not None and (
        recorded_source.key.harness != replay_harness
        or recorded_source.key.native_session_id != source.harness_session_id
        or recorded_source.ref.chat_id != source.source_chat_id
        or source.source_ref.strip()
        not in {recorded_source.ref.chat_id, recorded_source.key.native_session_id}
    ):
        raise ValueError("Continue replay identity differs from recorded native source")

    session = SessionRequest(
        requested_harness_session_id=source.harness_session_id,
        initial_model_selection=intent.initial_model_selection,
        conversation_intent=intent.selection,
        recorded_native_source=recorded_source,
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
    )

    return ContinueReplayContract(
        model=intent.selection.routing_token,
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
    "ContinueReplayIntent",
    "ContinueReplayRefused",
    "ContinueReplaySource",
    "build_continue_replay_contract",
    "continue_replay_source_from_reference",
    "select_continue_replay_intent",
]
