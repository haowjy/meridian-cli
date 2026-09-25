"""Value-only intent selection and assembly; no legacy effect collection here."""

from dataclasses import replace

import pytest

from meridian.lib.launch.continue_replay import (
    ContinueReplayIntent,
    ContinueReplayRefused,
    ContinueReplaySource,
    build_continue_replay_contract,
    select_continue_replay_intent,
)
from meridian.lib.state.session_authority import ConversationModelSelection


def source() -> ContinueReplaySource:
    return ContinueReplaySource(
        source_ref="native",
        harness_session_id="native",
        harness="pi",
        source_chat_id=None,
        source_work_id=None,
        source_execution_cwd=None,
        source_control_root=None,
        source_claude_config_dir=None,
        source_pi_session_dir=None,
        source_launch_policy_snapshot=None,
        tracked=False,
    )


@pytest.mark.parametrize("override", ["model", "", "  "])
def test_explicit_presence_not_truthiness(override: str) -> None:
    result = select_continue_replay_intent(operation="resume", requested_model_override=override)
    assert isinstance(result, ContinueReplayIntent)
    assert result.selection.requested_token == override
    assert result.selection.selection_source == "explicit_override"
    assert build_continue_replay_contract(source=source(), intent=result).model == override


def test_fork_ignores_resume_observation_and_invocation() -> None:
    initial = ConversationModelSelection(
        requested_token="initial", selection_source="initial_launch"
    )
    result = select_continue_replay_intent(
        operation="fork",
        fallback=initial,
        legacy_observation=ConversationModelSelection(
            requested_token="observed",
            selection_source="observed_last_used",
        ),
        legacy_invocation=initial,
    )
    assert isinstance(result, ContinueReplayIntent)
    assert result.selection == initial
    assert result.initial_model_selection is None


def test_no_intent_is_not_default_and_snapshot_is_not_selection() -> None:
    assert isinstance(select_continue_replay_intent(operation="resume"), ContinueReplayRefused)


def test_intent_and_builder_return_detached_provenance() -> None:
    selection = ConversationModelSelection(
        requested_token="value",
        selection_source="initial_launch",
        provenance={"source": "original"},
    )
    intent = ContinueReplayIntent(selection)
    selection.provenance["source"] = "input-mutated"
    intent.selection.provenance["source"] = "output-mutated"
    first = build_continue_replay_contract(source=source(), intent=intent)
    assert first.session.conversation_intent is not None
    first.session.conversation_intent.provenance["source"] = "contract-mutated"
    second = build_continue_replay_contract(source=source(), intent=intent)
    assert second.session.conversation_intent is not None
    assert second.session.conversation_intent.provenance == {"source": "original"}


def test_harness_and_agent_prohibitions_remain_in_pure_builder() -> None:
    intent = ContinueReplayIntent(ConversationModelSelection(selection_source="unknown"))
    with pytest.raises(ValueError, match="across harnesses"):
        build_continue_replay_contract(source=source(), intent=intent, explicit_harness="codex")
    with pytest.raises(ValueError, match="agent opt-out"):
        build_continue_replay_contract(source=source(), intent=intent, agent_opt_out=True)
    with pytest.raises(ValueError, match="--agent"):
        build_continue_replay_contract(source=source(), intent=intent, requested_agent="other")
    fork = build_continue_replay_contract(
        source=replace(source(), source_chat_id="c1"),
        intent=intent,
        fork=True,
    )
    assert fork.session.continue_fork and fork.session.forked_from_chat_id == "c1"
