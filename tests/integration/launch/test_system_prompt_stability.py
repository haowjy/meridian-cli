"""The system prompt belongs to the conversation, not to one launch of it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.context import LaunchContext
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    SessionRequest,
    SpawnRequest,
)
from meridian.lib.launch.session_links import (
    session_task_link_path,
    session_work_link_path,
)
from meridian.lib.state.spawn_store import start_spawn
from tests.support.fixtures import allow_headless_claude, write_minimal_mars_config
from tests.support.launch import build_launch_context, stub_bundle_request_and_resolve

_MODEL = "claude-haiku-5-5"
_SESSION_ID = "650ba0f7-6f5b-4a2f-9c1e-3f2b6b0c5a11"

pytestmark = pytest.mark.slow


def _native_store(project_root: Path) -> Path:
    """A Claude transcript a tracked continue can resolve its source from."""

    store = project_root / "claude-store"
    store.mkdir()
    (store / f"{_SESSION_ID}.jsonl").write_text(
        json.dumps({"sessionId": _SESSION_ID}) + "\n", encoding="utf-8"
    )
    return store


def _bind(
    project_root: Path,
    *,
    surface: LaunchCompositionSurface,
    spawn_id: str,
    goal: str | None = None,
    launch_policy_snapshot: LaunchPolicySnapshot | None = None,
    session: SessionRequest | None = None,
    dry_run: bool = True,
) -> LaunchContext:
    return build_launch_context(
        spawn_id=spawn_id,
        request=SpawnRequest(
            prompt="task prompt",
            model=_MODEL,
            harness=HarnessId.CLAUDE.value,
            goal=goal,
            launch_policy_snapshot=launch_policy_snapshot,
            session=session or SessionRequest(),
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.SPEC_ONLY,
            composition_surface=surface,
            runtime_root=(project_root / ".meridian").as_posix(),
            project_paths_project_root=project_root.as_posix(),
            project_paths_execution_cwd=project_root.as_posix(),
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=dry_run,
    )


def _system_prompt(context: LaunchContext) -> str:
    system_prompt = context.binding.run_params.appended_system_prompt
    assert system_prompt is not None
    return system_prompt


def _run_block(context: LaunchContext) -> str:
    return context.binding.spec.user_turn_content or ""


def test_system_prompt_is_stable_across_launches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A continue keeps the bytes; a new goal or surface changes them.

    The run ID, the launching chat, a spawn's goal and a spawn's per-chat links are
    per-launch, so they live in the user turn. Rebuilding them into the system prompt
    made every continue miss the harness prompt cache.
    """

    write_minimal_mars_config(tmp_path)
    allow_headless_claude(tmp_path)
    stub_bundle_request_and_resolve(monkeypatch, model=_MODEL, harness=HarnessId.CLAUDE)

    fresh = _bind(
        tmp_path,
        surface=LaunchCompositionSurface.SPAWN_PREPARE,
        spawn_id="p1",
        goal="ship the feature",
    )
    snapshot = fresh.resolved_request.launch_policy_snapshot
    assert snapshot is not None
    continued = _bind(
        tmp_path,
        surface=LaunchCompositionSurface.SPAWN_PREPARE,
        spawn_id="p2",
        goal="ship the feature",
        launch_policy_snapshot=snapshot,
        session=SessionRequest(
            requested_harness_session_id=_SESSION_ID,
            continue_harness=HarnessId.CLAUDE.value,
            continue_source_ref="p1",
            continue_source_tracked=True,
            continue_chat_id="c1",
            source_native_store=_native_store(tmp_path).as_posix(),
        ),
    )
    assert _system_prompt(continued) == _system_prompt(fresh)

    # Identity is not achieved by ignoring inputs: a primary's goal is conversation
    # identity, and the surface decides the preamble.
    primary_goal = _system_prompt(
        _bind(
            tmp_path,
            surface=LaunchCompositionSurface.PRIMARY,
            spawn_id="dry-run-primary",
            goal="ship the feature",
        )
    )
    other_primary_goal = _system_prompt(
        _bind(
            tmp_path,
            surface=LaunchCompositionSurface.PRIMARY,
            spawn_id="dry-run-primary",
            goal="fix the bug",
        )
    )
    primary = _system_prompt(
        _bind(
            tmp_path,
            surface=LaunchCompositionSurface.PRIMARY,
            spawn_id="dry-run-primary",
        )
    )
    spawn = _system_prompt(
        _bind(
            tmp_path,
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            spawn_id="p3",
        )
    )
    assert primary_goal != other_primary_goal
    assert primary != spawn
    # A spawn's goal travels with its task, not the shared prefix: two spawns of the
    # same agent must send the same system bytes whatever their goals.
    assert spawn == _system_prompt(fresh)

    # A live spawn names its per-chat links in the user turn. Bind two real spawns on
    # different chats: the run blocks carry different links, the system bytes do not.
    runtime_root = tmp_path / ".meridian"
    for spawn_id, chat_id in (("p10", "c10"), ("p11", "c11")):
        start_spawn(
            runtime_root,
            chat_id=chat_id,
            model=_MODEL,
            agent="coder",
            harness=HarnessId.CLAUDE.value,
            prompt="task prompt",
            spawn_id=spawn_id,
        )
    linked_first = _bind(
        tmp_path,
        surface=LaunchCompositionSurface.SPAWN_PREPARE,
        spawn_id="p10",
        dry_run=False,
    )
    linked_second = _bind(
        tmp_path,
        surface=LaunchCompositionSurface.SPAWN_PREPARE,
        spawn_id="p11",
        dry_run=False,
    )
    assert _system_prompt(linked_first) == _system_prompt(linked_second)
    assert session_work_link_path(runtime_root, "c10").as_posix() in _run_block(linked_first)
    assert session_task_link_path(runtime_root, "c11").as_posix() in _run_block(linked_second)
    assert _run_block(linked_first) != _run_block(linked_second)
