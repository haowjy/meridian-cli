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
from meridian.lib.launch.run_reopen import bind_reopened_run
from meridian.lib.launch.session_scope import SessionAttempt, session_scope
from meridian.lib.launch.types import PrimarySessionMetadata
from meridian.lib.state import spawn_store
from meridian.lib.state.session_store import (
    ConversationModelSelection,
    get_model_selection,
    get_session_record,
    start_session,
    stop_session,
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


def _metadata() -> PrimarySessionMetadata:
    return PrimarySessionMetadata(
        harness="codex",
        model="test",
        agent="",
        agent_path="",
        skills=(),
        skill_paths=(),
    )


def test_fresh_session_scope_binds_the_run_generation(tmp_path: Path) -> None:
    run_id = spawn_store.start_spawn(
        tmp_path,
        spawn_id="p1",
        chat_id="c-pending",
        model="test",
        agent="",
        harness="codex",
        prompt="fresh",
    )

    with session_scope(
        runtime_root=tmp_path,
        metadata=_metadata(),
        request=SessionRequest(),
        spawn_id=run_id,
    ) as managed:
        row = spawn_store.get_spawn(tmp_path, run_id)
        session = get_session_record(tmp_path, managed.chat_id)
        assert row is not None
        assert session is not None
        assert (row.chat_id, row.session_instance_id) == (
            managed.chat_id,
            managed.session_instance_id,
        )
        assert (session.spawn_id, session.session_instance_id) == (
            str(row.id),
            row.session_instance_id,
        )


def test_reopen_helper_binds_chat_generation_lineage_and_work(tmp_path: Path) -> None:
    source_chat = start_session(
        tmp_path,
        harness="codex",
        harness_session_id="native",
        model="test",
        chat_id="c1",
    )
    source = get_session_record(tmp_path, source_chat)
    assert source is not None
    stop_session(tmp_path, source_chat)
    run_id = spawn_store.start_spawn(
        tmp_path,
        spawn_id="p1.2",
        chat_id=source_chat,
        model="test",
        agent="",
        harness="codex",
        prompt="reopen",
    )
    request = SessionRequest(
        continue_chat_id=source_chat,
        primary_session_mode="resume",
        forked_from_history_id=source.history_id,
    )

    with session_scope(
        runtime_root=tmp_path,
        metadata=_metadata(),
        request=request,
        chat_id=source_chat,
        spawn_id=run_id,
    ) as managed:
        bind_reopened_run(tmp_path, run_id, managed, work_id="w1")
        row = spawn_store.get_spawn(tmp_path, run_id)
        session = get_session_record(tmp_path, managed.chat_id)
        assert row is not None
        assert session is not None
        assert (row.chat_id, row.session_instance_id, row.work_id) == (
            managed.chat_id,
            managed.session_instance_id,
            "w1",
        )
        assert (session.spawn_id, session.session_instance_id) == (
            str(row.id),
            row.session_instance_id,
        )


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
