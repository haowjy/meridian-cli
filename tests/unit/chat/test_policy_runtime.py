from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.bootstrap.services import build_chat_entrypoint, prepare_for_runtime_write
from meridian.lib.chat.event_log import ChatEventLog
from meridian.lib.chat.policy import (
    ChatPolicySnapshot,
    ChatPromptDocumentSnapshot,
    ChatPromptInputsSnapshot,
    default_chat_policy_snapshot,
)
from meridian.lib.chat.runtime import (
    ChatRuntime,
    ChatRuntimeRequest,
    build_chat_runtime,
    build_chat_runtime_from_entrypoint,
)


class _NoopAcquisition:
    async def acquire(self, *args, **kwargs):  # pragma: no cover - not called in this suite
        raise AssertionError("acquire should not be called")


def _snapshot(*, model: str, skill_content: str) -> ChatPolicySnapshot:
    base = default_chat_policy_snapshot(model=model)
    return base.model_copy(
        update={
            "snapshot_id": f"snap-{model}",
            "prompt_inputs": ChatPromptInputsSnapshot(
                skill_documents=(
                    ChatPromptDocumentSnapshot(
                        kind="skill",
                        logical_name="frozen-skill",
                        path=f"/skills/{model}.md",
                        content=skill_content,
                    ),
                ),
                agent_profile_body=skill_content,
                adhoc_agent_payload=f"adhoc::{skill_content}",
            ),
        }
    )


def test_build_chat_runtime_from_typed_request(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    project_root = tmp_path / "project"
    runtime = build_chat_runtime(
        ChatRuntimeRequest(
            runtime_root=runtime_root,
            project_root=project_root,
            default_policy_snapshot=_snapshot(model="gpt-a", skill_content="alpha"),
            backend_acquisition=_NoopAcquisition(),
        )
    )

    assert runtime.runtime_root == runtime_root
    assert runtime.project_root == project_root


def test_build_chat_runtime_from_entrypoint_uses_shared_roots(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    prepared = prepare_for_runtime_write(project_root)
    entrypoint = build_chat_entrypoint(prepared)

    runtime = build_chat_runtime_from_entrypoint(
        entrypoint=entrypoint,
        default_policy_snapshot=_snapshot(model="gpt-a", skill_content="alpha"),
        backend_acquisition=_NoopAcquisition(),
    )

    assert runtime.project_root == project_root
    assert runtime.runtime_root == prepared.runtime_root


def test_build_chat_runtime_from_entrypoint_blocks_nested_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    prepared = prepare_for_runtime_write(project_root)
    entrypoint = build_chat_entrypoint(prepared)
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")

    with pytest.raises(ValueError, match="blocked in nested/delegated Meridian execution"):
        build_chat_runtime_from_entrypoint(
            entrypoint=entrypoint,
            default_policy_snapshot=_snapshot(model="gpt-a", skill_content="alpha"),
            backend_acquisition=_NoopAcquisition(),
        )


@pytest.mark.asyncio
async def test_chat_runtime_persists_policy_snapshot_on_create_before_first_acquire(
    tmp_path: Path,
) -> None:
    runtime = ChatRuntime(
        runtime_root=tmp_path,
        project_root=tmp_path,
        default_policy_snapshot=_snapshot(model="gpt-a", skill_content="alpha"),
        backend_acquisition=_NoopAcquisition(),
    )
    await runtime.start()
    try:
        view = await runtime.create_chat()
        chat_id = view.chat_id
        assert runtime.paths.chat_policy_path(chat_id).exists()
        stored = runtime.get_policy_snapshot(chat_id)
    finally:
        await runtime.stop()

    assert stored.canonical_model_id == "gpt-a"
    assert stored.prompt_inputs.agent_profile_body == "alpha"
    assert stored.prompt_inputs.skill_documents[0].content == "alpha"
    assert stored.prompt_inputs.adhoc_agent_payload == "adhoc::alpha"


@pytest.mark.asyncio
async def test_chat_runtime_recovery_uses_persisted_snapshot_not_new_runtime_default(
    tmp_path: Path,
) -> None:
    first = ChatRuntime(
        runtime_root=tmp_path,
        project_root=tmp_path,
        default_policy_snapshot=_snapshot(model="gpt-a", skill_content="alpha"),
        backend_acquisition=_NoopAcquisition(),
    )
    await first.start()
    try:
        chat_id = (await first.create_chat()).chat_id
    finally:
        await first.stop()

    restarted = ChatRuntime(
        runtime_root=tmp_path,
        project_root=tmp_path,
        default_policy_snapshot=_snapshot(model="gpt-b", skill_content="beta"),
        backend_acquisition=_NoopAcquisition(),
    )
    await restarted.start()
    try:
        restored = restarted.get_policy_snapshot(chat_id)
    finally:
        await restarted.stop()

    assert restored.canonical_model_id == "gpt-a"
    assert restored.prompt_inputs.agent_profile_body == "alpha"
    assert restored.prompt_inputs.skill_documents[0].content == "alpha"
    assert restored.prompt_inputs.adhoc_agent_payload == "adhoc::alpha"


@pytest.mark.asyncio
async def test_chat_runtime_recovery_marks_chat_unavailable_when_snapshot_missing(
    tmp_path: Path,
) -> None:
    first = ChatRuntime(
        runtime_root=tmp_path,
        project_root=tmp_path,
        default_policy_snapshot=_snapshot(model="gpt-a", skill_content="alpha"),
        backend_acquisition=_NoopAcquisition(),
    )
    await first.start()
    try:
        chat_id = (await first.create_chat()).chat_id
    finally:
        await first.stop()

    first.paths.chat_policy_path(chat_id).unlink()

    restarted = ChatRuntime(
        runtime_root=tmp_path,
        project_root=tmp_path,
        default_policy_snapshot=_snapshot(model="gpt-b", skill_content="beta"),
        backend_acquisition=_NoopAcquisition(),
    )
    await restarted.start()
    try:
        assert restarted.get_state(chat_id) == "unavailable"
        with pytest.raises(RuntimeError, match="snapshot missing"):
            restarted.get_policy_snapshot(chat_id)
        events = list(ChatEventLog(restarted.paths.chat_history_path(chat_id)).read_all())
    finally:
        await restarted.stop()

    assert events[-1].type == "runtime.error"
    assert events[-1].payload.get("reason") == "policy_snapshot_unavailable"
    assert "snapshot missing" in str(events[-1].payload.get("detail"))


@pytest.mark.asyncio
async def test_chat_runtime_recovery_marks_chat_unavailable_when_snapshot_malformed(
    tmp_path: Path,
) -> None:
    first = ChatRuntime(
        runtime_root=tmp_path,
        project_root=tmp_path,
        default_policy_snapshot=_snapshot(model="gpt-a", skill_content="alpha"),
        backend_acquisition=_NoopAcquisition(),
    )
    await first.start()
    try:
        chat_id = (await first.create_chat()).chat_id
    finally:
        await first.stop()

    first.paths.chat_policy_path(chat_id).write_text("{not-json}\n", encoding="utf-8")

    restarted = ChatRuntime(
        runtime_root=tmp_path,
        project_root=tmp_path,
        default_policy_snapshot=_snapshot(model="gpt-b", skill_content="beta"),
        backend_acquisition=_NoopAcquisition(),
    )
    await restarted.start()
    try:
        assert restarted.get_state(chat_id) == "unavailable"
        with pytest.raises(RuntimeError, match="snapshot invalid"):
            restarted.get_policy_snapshot(chat_id)
        events = list(ChatEventLog(restarted.paths.chat_history_path(chat_id)).read_all())
    finally:
        await restarted.stop()

    assert events[-1].type == "runtime.error"
    assert events[-1].payload.get("reason") == "policy_snapshot_unavailable"
    assert "snapshot invalid" in str(events[-1].payload.get("detail"))
