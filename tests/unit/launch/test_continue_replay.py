"""Unit tests for exact-continue replay contract builders."""

from __future__ import annotations

from pathlib import Path

import pytest

import meridian.lib.launch.continue_replay as continue_replay_module
from meridian.lib.core.execution_policy import ResolvedExecutionPolicy
from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.launch.continue_replay import (
    ContinueReplaySource,
    ConversationModelSelection,
    build_continue_replay_contract,
    continue_replay_source_from_reference,
)
from meridian.lib.ops.reference import ResolvedSessionReference
from meridian.lib.ops.reference_recovery import RecoveryProvenance, RecoveryResult


def _snapshot() -> LaunchPolicySnapshot:
    return LaunchPolicySnapshot(
        model="claude-sonnet-4-6",
        harness="claude",
        agent="agent-a",
        skills=("skill-a",),
        execution_policy=ResolvedExecutionPolicy(approval="auto"),
        extra_args=("--permission-mode", "acceptEdits"),
    )


def test_build_continue_replay_contract_from_snapshot() -> None:
    snapshot = _snapshot()
    source = ContinueReplaySource(
        source_ref="p41",
        harness_session_id="session-41",
        harness="claude",
        source_chat_id="c41",
        source_work_id="source-work",
        source_execution_cwd="/tmp/source",
        source_control_root="/tmp/repo",
        source_native_store="/recorded/store",
        source_launch_policy_snapshot=snapshot,
        tracked=True,
        source_model="ignored-live-model",
        source_agent="ignored-agent",
        source_skills=("ignored-skill",),
    )

    contract = build_continue_replay_contract(source=source)

    assert contract.launch_policy_snapshot == snapshot
    assert contract.work_id == "source-work"
    assert contract.task_dir == "/tmp/source"
    assert contract.harness == "claude"
    assert contract.model == "claude-sonnet-4-6"
    assert contract.agent == "agent-a"
    assert contract.agent_opt_out is False
    assert contract.skills == ("skill-a",)
    assert contract.passthrough_args == snapshot.extra_args
    assert contract.session.requested_harness_session_id == "session-41"
    assert contract.session.continue_source_ref == "p41"
    assert contract.session.continue_chat_id == "c41"
    assert contract.session.source_execution_cwd == "/tmp/source"


def test_continue_replay_source_from_reference_uses_authoritative_session_id() -> None:
    resolved = ResolvedSessionReference(
        harness_session_id=None,
        harness="claude",
        source_chat_id="c41",
        source_model="claude-sonnet-4-6",
        source_agent="agent-a",
        source_skills=("skill-a",),
        source_work_id="source-work",
        tracked=True,
        source_execution_cwd="/tmp/source",
        source_launch_policy_snapshot=None,
        recovery=RecoveryResult(
            harness_session_id="recovered-session",
            provenance=RecoveryProvenance.SESSION_STORE,
        ),
    )

    contract = build_continue_replay_contract(
        source=continue_replay_source_from_reference(
            "p41",
            resolved,
            harness_session_id=resolved.authoritative_harness_session_id,
        ),
    )

    assert contract.session.requested_harness_session_id == "recovered-session"
    assert contract.work_id == "source-work"
    assert contract.task_dir == "/tmp/source"
    assert contract.model == "claude-sonnet-4-6"
    assert contract.agent is None
    assert contract.skills == ()


def test_build_continue_replay_contract_legacy_empty_model_override() -> None:
    snapshot = LaunchPolicySnapshot(model="", harness="codex", agent="tech-lead")
    source = ContinueReplaySource(
        source_ref="p44",
        harness_session_id="session-44",
        harness="codex",
        source_chat_id="c44",
        source_work_id=None,
        source_execution_cwd=None,
        source_control_root=None,
        source_launch_policy_snapshot=snapshot,
        tracked=True,
    )

    contract = build_continue_replay_contract(source=source)

    assert contract.model is None
    assert contract.launch_policy_snapshot == snapshot


def test_build_continue_replay_contract_uses_snapshot_harness_when_reference_has_none() -> None:
    snapshot = LaunchPolicySnapshot(model="", harness="codex", agent="tech-lead")
    source = ContinueReplaySource(
        source_ref="p44",
        harness_session_id="session-44",
        harness=None,
        source_chat_id="c44",
        source_work_id=None,
        source_execution_cwd=None,
        source_control_root=None,
        source_launch_policy_snapshot=snapshot,
        tracked=True,
    )

    contract = build_continue_replay_contract(source=source)

    assert contract.harness == "codex"


def test_build_continue_replay_contract_rejects_harness_conflict() -> None:
    snapshot = LaunchPolicySnapshot(model="", harness="codex")
    source = ContinueReplaySource(
        source_ref="p44",
        harness_session_id="session-44",
        harness="claude",
        source_chat_id="c44",
        source_work_id=None,
        source_execution_cwd=None,
        source_control_root=None,
        source_launch_policy_snapshot=snapshot,
        tracked=True,
    )

    with pytest.raises(ValueError, match="Cannot continue across harnesses"):
        build_continue_replay_contract(source=source)


def test_build_continue_replay_contract_rejects_agent_opt_out() -> None:
    source = ContinueReplaySource(
        source_ref="p44",
        harness_session_id="session-44",
        harness="codex",
        source_chat_id="c44",
        source_work_id=None,
        source_execution_cwd=None,
        source_control_root=None,
        source_launch_policy_snapshot=None,
        tracked=True,
    )

    with pytest.raises(ValueError, match="agent opt-out"):
        build_continue_replay_contract(source=source, agent_opt_out=True)


def _continue_source() -> ContinueReplaySource:
    return ContinueReplaySource(
        source_ref="p41",
        harness_session_id="session-41",
        harness="claude",
        source_chat_id="c41",
        source_work_id=None,
        source_execution_cwd="/tmp/source",
        source_control_root="/tmp/repo",
        source_native_store="/recorded/store",
        source_launch_policy_snapshot=_snapshot(),
        tracked=True,
    )


def _recorded_selection(token: str) -> ConversationModelSelection:
    return ConversationModelSelection(
        requested_token=token,
        selected_token=token,
        canonical_model_id=token,
        harness_model_id=token,
        model_mode="named",
        selection_source="recorded_selection",
    )


def _patch_intent_seams(
    monkeypatch: pytest.MonkeyPatch,
    *,
    recorded: ConversationModelSelection | None,
    live: str | None,
    stored: str | None,
) -> None:
    monkeypatch.setattr(
        continue_replay_module, "get_model_selection", lambda *a, **k: recorded
    )
    monkeypatch.setattr(
        continue_replay_module, "get_initial_model_selection", lambda *a, **k: None
    )
    monkeypatch.setattr(
        continue_replay_module, "read_last_executed_model", lambda *a, **k: live
    )
    monkeypatch.setattr(
        continue_replay_module, "get_last_executed_model", lambda *a, **k: stored
    )
    monkeypatch.setattr(
        continue_replay_module, "record_model_observation", lambda *a, **k: True
    )
    monkeypatch.setattr(
        continue_replay_module, "run_mars_models_resolve", lambda *a, **k: {"harness": "claude"}
    )


def test_explicit_override_beats_observed(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _continue_source()
    _patch_intent_seams(
        monkeypatch,
        recorded=_recorded_selection("recorded-token"),
        live="observed-token",
        stored=None,
    )
    live_calls: list[str] = []
    monkeypatch.setattr(
        continue_replay_module,
        "read_last_executed_model",
        lambda *a, **k: live_calls.append("live") or "observed-token",
    )

    contract = build_continue_replay_contract(
        source=source, requested_model_override="explicit-token", runtime_root=Path("/tmp/x")
    )

    assert live_calls == []
    assert contract.model == "explicit-token"
    assert contract.session.conversation_intent.selection_source == "explicit_override"


def test_observed_beats_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _continue_source()
    _patch_intent_seams(
        monkeypatch,
        recorded=_recorded_selection("recorded-token"),
        live="observed-token",
        stored=None,
    )

    contract = build_continue_replay_contract(source=source, runtime_root=Path("/tmp/x"))

    assert contract.model == "observed-token"
    assert contract.session.conversation_intent.selection_source == "observed_last_used"


def test_stored_observation_beats_recorded_when_live_read_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _continue_source()
    _patch_intent_seams(
        monkeypatch,
        recorded=_recorded_selection("recorded-token"),
        live=None,
        stored="stored-token",
    )

    contract = build_continue_replay_contract(source=source, runtime_root=Path("/tmp/x"))

    assert contract.model == "stored-token"
    assert contract.session.conversation_intent.selection_source == "observed_last_used"


def test_unroutable_observed_falls_back_to_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _continue_source()
    _patch_intent_seams(
        monkeypatch,
        recorded=_recorded_selection("recorded-token"),
        live="opencode/deepseek-v4-flash-free",
        stored=None,
    )
    monkeypatch.setattr(
        continue_replay_module, "run_mars_models_resolve", lambda *a, **k: None
    )

    contract = build_continue_replay_contract(source=source, runtime_root=Path("/tmp/x"))

    assert contract.model == "recorded-token"
    assert contract.session.conversation_intent.selection_source == "recorded_selection"


def test_observed_routing_to_other_harness_falls_back_to_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _continue_source()
    _patch_intent_seams(
        monkeypatch,
        recorded=_recorded_selection("recorded-token"),
        live="observed-token",
        stored=None,
    )
    monkeypatch.setattr(
        continue_replay_module,
        "run_mars_models_resolve",
        lambda *a, **k: {"route": {"harness": "opencode"}},
    )

    contract = build_continue_replay_contract(source=source, runtime_root=Path("/tmp/x"))

    assert contract.model == "recorded-token"
    assert contract.session.conversation_intent.selection_source == "recorded_selection"


def test_fork_ignores_observed(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _continue_source()
    live_calls: list[str] = []
    monkeypatch.setattr(
        continue_replay_module,
        "read_last_executed_model",
        lambda *a, **k: live_calls.append("live") or "observed-token",
    )
    monkeypatch.setattr(
        continue_replay_module,
        "get_model_selection",
        lambda *a, **k: _recorded_selection("recorded-token"),
    )
    monkeypatch.setattr(
        continue_replay_module, "get_initial_model_selection", lambda *a, **k: None
    )

    contract = build_continue_replay_contract(source=source, fork=True, runtime_root=Path("/tmp/x"))

    assert live_calls == []
    assert contract.model == "claude-sonnet-4-6"
    assert contract.session.conversation_intent.selection_source == "initial_launch"
