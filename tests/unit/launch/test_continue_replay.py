"""Unit tests for exact-continue replay contract builders."""

from __future__ import annotations

import pytest

from meridian.lib.core.execution_policy import ResolvedExecutionPolicy
from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.launch.continue_replay import (
    ContinueReplaySource,
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
        source_claude_config_dir=None,
        source_pi_session_dir=None,
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
        source_claude_config_dir=None,
        source_pi_session_dir=None,
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
        source_claude_config_dir=None,
        source_pi_session_dir=None,
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
        source_claude_config_dir=None,
        source_pi_session_dir=None,
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
        source_claude_config_dir=None,
        source_pi_session_dir=None,
        source_launch_policy_snapshot=None,
        tracked=True,
    )

    with pytest.raises(ValueError, match="agent opt-out"):
        build_continue_replay_contract(source=source, agent_opt_out=True)
