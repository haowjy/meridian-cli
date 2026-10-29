"""Public fork composition must preserve the recorded native namespace."""
from meridian.lib.ops.reference import ResolvedSessionReference
from meridian.lib.ops.spawn.api import _build_fork_create_input
from meridian.lib.ops.spawn.models import SpawnForkInput


def test_fork_request_preserves_recorded_store() -> None:
    reference = ResolvedSessionReference(
        harness_session_id="12345678-1234-4234-8234-123456789abc",
        harness="codex", source_chat_id="c1", source_model=None, source_agent=None,
        source_skills=(), source_work_id=None, tracked=True,
        source_native_store="/recorded/codex/sessions",
    )
    request = _build_fork_create_input(
        payload=SpawnForkInput(source_ref="c1", prompt="continue"),
        normalized_source_ref="c1", resolved_reference=reference,
        requested_model="", requested_agent=None, inherited_skills=(),
        requested_work="", requested_task_dir=None, requested_goal=None, harness="codex",
    )
    assert request.session.source_native_store == reference.source_native_store
