"""Effect collection for legacy replay and a private, not owner-wired exact seam."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast

from meridian.lib.catalog.model_aliases import run_mars_models_resolve
from meridian.lib.core.types import HarnessSessionId
from meridian.lib.harness.model_observation import NativeModelReadContext, read_last_executed_model
from meridian.lib.launch.continue_replay import (
    ContinueReplayIntent,
    ContinueReplayRefused,
    ContinueReplaySource,
    _reject_exact_continue_agent_override,
    _resolve_replay_harness,
    select_continue_replay_intent,
)
from meridian.lib.launch.launch_types import CompositionWarning
from meridian.lib.launch.policy_snapshot import managed_model_override_from_persisted_model
from meridian.lib.state.event_store import utc_now_iso
from meridian.lib.state.session_authority import (
    ConversationModelSelection,
    ExactModelEvidence,
    ExactModelObservation,
    FactsUnavailable,
    ModelSourceConflict,
    RecordedNativeSource,
)
from meridian.lib.state.session_store import (
    SessionModelObservationEvent,
    get_initial_model_selection,
    get_last_executed_model,
    get_model_selection,
    record_model_observation,
)

if TYPE_CHECKING:
    from meridian.lib.ops.reference import AuthorizedSourceMetadata, AuthorizedSourceUse


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
    return any(_payload_harness(path) == harness for path in cast("list[object]", runnable_paths))


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
    token = (
        live
        if live is not None
        else get_last_executed_model(runtime_root, replay_harness, harness_session_id)
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


def collect_untracked_legacy_continue_intent(
    *,
    source: ContinueReplaySource,
    runtime_root: Path | None,
    explicit_harness: str | None = None,
    requested_agent: str | None = None,
    agent_opt_out: bool = False,
    fork: bool = False,
    requested_model_override: str | None = None,
) -> ContinueReplayIntent:
    """Preserve legacy effects only; callers still own strict negative admission.

    This is not an exact-source fallback. Never pass an admitted recorded source.
    Historical legacy metadata may still carry ``tracked``; it is not authority.
    """
    if source.recorded_native_source is not None:
        raise ValueError("Recorded sources cannot use legacy model collection")
    _reject_exact_continue_agent_override(
        requested_agent=requested_agent, agent_opt_out=agent_opt_out
    )
    harness = _resolve_replay_harness(source=source, explicit_harness=explicit_harness)
    recorded = (
        get_model_selection(runtime_root, harness, source.harness_session_id)
        if runtime_root is not None and source.harness_session_id is not None and not fork
        else None
    )
    seed = (
        get_initial_model_selection(
            runtime_root, harness, source.harness_session_id, source_chat_id=source.source_chat_id
        )
        if recorded is None
        and runtime_root is not None
        and source.harness_session_id is not None
        and not fork
        else None
    )
    observed = None
    if requested_model_override is None and not fork and runtime_root is not None:
        token = _observed_last_executed_model(
            source=source, replay_harness=harness, runtime_root=runtime_root
        )
        if token is not None:
            observed = ConversationModelSelection(
                requested_token=token, selection_source="observed_last_used"
            )
    snapshot_model = (
        managed_model_override_from_persisted_model(source.source_launch_policy_snapshot.model)
        if source.source_launch_policy_snapshot is not None
        else (source.source_model or "").strip() or None
    )
    fallback = (
        None
        if source.tracked and runtime_root is not None and not fork
        else _fallback_conversation_intent(source=source, snapshot_model=snapshot_model)
    )
    result = select_continue_replay_intent(
        operation="fork" if fork else "resume",
        requested_model_override=requested_model_override,
        legacy_invocation=recorded,
        legacy_observation=observed,
        recovered_seed=seed,
        fallback=fallback,
    )
    if isinstance(result, ContinueReplayRefused):
        raise ValueError(result.message)
    return result


class _ExactEvidenceReader(Protocol):
    def __call__(
        self, source: RecordedNativeSource, *, view: Literal["reopen-default", "process-active"]
    ) -> ExactModelEvidence: ...


PAIR_FIDELITY_WARNING = (
    "The selected Pi reopen setting cannot be preserved through managed model routing. "
    "Using recorded model intent instead; this may select a different provider or model."
)


@dataclass(frozen=True)
class _ExactContinueCollection:
    intent: ContinueReplayIntent
    evidence: ExactModelEvidence | None = None
    disposition: Literal["pair_projection_unproven"] | None = None
    warnings: tuple[CompositionWarning, ...] = ()


def _collect_exact_continue_intent(
    admitted: AuthorizedSourceUse,
    metadata: AuthorizedSourceMetadata,
    *,
    requested_model_override: str | None,
    view: Literal["reopen-default", "process-active"],
    reader: _ExactEvidenceReader,
) -> _ExactContinueCollection | ContinueReplayRefused:
    """Synthetic downstream seam, NOT native-entry or raw-vector authorization.

    No production caller owns a qualified persisted reopen entry yet. A future
    owner must admit original AND saved raw vectors before metadata/history,
    and qualify the entry independently. These DTOs do not certify that work.
    """
    if metadata.admitted != admitted:
        return ContinueReplayRefused(
            "metadata_mismatch", "Exact replay metadata differs from admitted source use."
        )
    if admitted.operation != "resume" or view != "reopen-default":
        return ContinueReplayRefused(
            "unsupported_view",
            "Exact replay requires a persisted reopen view, not an active cursor or fork.",
        )
    source = admitted.source
    if source.key.harness != "pi":
        return ContinueReplayRefused(
            "unsupported_provider", "Exact model provider is not qualified."
        )
    facts = admitted.authority.replay_model_facts(source)
    if isinstance(facts, FactsUnavailable):
        return ContinueReplayRefused(
            "source_conflict", "Exact accepted model facts conflict with the admitted source."
        )
    evidence = None
    disposition = None
    warnings = ()
    if requested_model_override is None:
        evidence = reader(source, view=view)
        if isinstance(evidence, ModelSourceConflict):
            return ContinueReplayRefused(
                "source_conflict", "Exact native model source conflicts with the admitted source."
            )
        if isinstance(evidence, ExactModelObservation):
            if (
                evidence.source != source
                or evidence.complete is not True
                or evidence.provider_contract != "pi-0.87.1-legacy-v3-settings-v1"
                or evidence.view_basis != view
                or evidence.model_basis != "selected_reopen_default"
                or not evidence.native_provider.strip()
                or not evidence.model_token.strip()
            ):
                return ContinueReplayRefused(
                    "evidence_mismatch",
                    "Exact native model evidence differs from the requested source/view contract.",
                )
            # Same-harness compatibility cannot prove pair-preserving emission.
            # Do not query Mars or fabricate a canonical identity from native IDs.
            disposition = "pair_projection_unproven"
            warnings = (
                CompositionWarning(code="pair_projection_unproven", message=PAIR_FIDELITY_WARNING),
            )
        elif evidence.reason in {
            "unsupported_provider",
            "unsupported_view",
            "unsupported_dialect",
        }:
            return ContinueReplayRefused(
                "unsupported_provider", "Exact model source/view contract is unsupported."
            )
    result = select_continue_replay_intent(
        operation=admitted.operation,
        requested_model_override=requested_model_override,
        exact_facts=facts,
        recorded_native_source=source,
    )
    if isinstance(result, ContinueReplayRefused):
        if disposition is not None:
            return ContinueReplayRefused(
                "missing_intent",
                "No pair-preserving projection or accepted model intent is available. "
                "Continue with an explicit --model.",
            )
        return result
    return _ExactContinueCollection(result, evidence, disposition, warnings)
