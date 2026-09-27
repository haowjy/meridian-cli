"""Regression coverage for the invocation model-selection recorded at primary start.

An OpenCode exact-continue/resume preserves the native session's committed model,
so the launch spec carries no model (see
``test_opencode_exact_continue_replay_drops_model_from_spec`` in
``tests/integration/cli/test_primary_continue.py``). The invocation event must
still record the source snapshot's executable identity; reconstructing "named"
from a null spec model previously failed ConversationModelSelection validation
and aborted the managed-primary attach.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.types import SpawnId
from meridian.lib.launch.request import SessionRequest
from meridian.lib.launch.session_scope import SessionAttempt
from meridian.lib.state.session_store import (
    ConversationModelSelection,
    get_model_selection,
    get_session_record,
    start_session,
)


def _snapshot(**overrides: object) -> LaunchPolicySnapshot:
    fields: dict[str, object] = {
        "model": "",
        "harness": "opencode",
        "model_selection_requested_token": "xai/grok-4.6",
        "model_selection_selected_token": "grok",
        "model_selection_canonical_id": "grok-4.6",
        "model_selection_harness_model_id": "xai/grok-4.6",
        "model_selection_provider_constraint": "xai",
        "field_provenance": {"resident_rearm_budget_source": "unset"},
    }
    fields.update(overrides)
    return LaunchPolicySnapshot.model_validate(fields)


def _record(
    runtime_root: Path,
    snapshot: LaunchPolicySnapshot,
    *,
    spec_model: str | None,
) -> ConversationModelSelection:
    chat_id = start_session(
        runtime_root,
        harness="opencode",
        harness_session_id="ses_source",
        model="grok-4.6",
        chat_id="c1",
        kind="primary",
        model_selection_protocol=1,
    )
    record = get_session_record(runtime_root, chat_id)
    assert record is not None
    context = SimpleNamespace(
        resolved_request=SimpleNamespace(
            launch_policy_snapshot=snapshot,
            session=SessionRequest(
                requested_harness_session_id="ses_source",
                continue_source_ref="ses_source",
                primary_session_mode="resume",
                conversation_intent=ConversationModelSelection(
                    requested_token="xai/grok-4.6",
                    selected_token="grok",
                    canonical_model_id="grok-4.6",
                    harness_model_id="xai/grok-4.6",
                    model_mode="named",
                    selection_source="recorded_selection",
                ),
            ),
        ),
        binding=SimpleNamespace(spec=SimpleNamespace(model=spec_model)),
        harness=SimpleNamespace(id="opencode"),
    )
    attempt = SessionAttempt(
        runtime_root, chat_id, record.session_instance_id, "attempt-1", SpawnId("p1")
    )
    attempt.record_started(context, "ses_source")  # type: ignore[arg-type]
    selection = get_model_selection(runtime_root, "opencode", "ses_source")
    assert selection is not None
    return selection


def test_resume_records_named_selection_from_source_snapshot_when_spec_has_no_model(
    tmp_path: Path,
) -> None:
    selection = _record(tmp_path, _snapshot(), spec_model=None)

    assert selection.model_mode == "named"
    assert selection.canonical_model_id == "grok-4.6"
    assert selection.harness_model_id == "xai/grok-4.6"


def test_spec_model_backfills_executable_identity_when_snapshot_lacks_it(
    tmp_path: Path,
) -> None:
    selection = _record(
        tmp_path,
        _snapshot(model_selection_harness_model_id=None),
        spec_model="xai/grok-4.6",
    )

    assert selection.model_mode == "named"
    assert selection.harness_model_id == "xai/grok-4.6"


def test_invocation_selection_falls_back_to_harness_default_when_identity_incomplete(
    tmp_path: Path,
) -> None:
    selection = _record(
        tmp_path,
        _snapshot(model_selection_harness_model_id=None),
        spec_model=None,
    )

    assert selection.model_mode == "harness_default"
    assert selection.canonical_model_id is None
    assert selection.harness_model_id is None
    assert selection.provider_constraint is None
