from __future__ import annotations

from pathlib import Path

import pytest  # noqa: TC002 — used at runtime (pytest.raises, fixtures)

from meridian.lib.core.types import ModelId
from meridian.lib.harness.claude import ClaudeAdapter
from meridian.lib.harness.ids import HarnessId
from meridian.lib.harness.launch_spec import ClaudeLaunchSpec, OpenCodeLaunchSpec
from meridian.lib.harness.opencode import OpenCodeAdapter
from meridian.lib.launch.command import build_launch_argv, resolve_launch_spec_stage
from meridian.lib.launch.reference import ReferenceItem
from meridian.lib.launch.run_inputs import ResolvedRunInputs
from meridian.lib.safety.permissions import PermissionConfig, TieredPermissionResolver


def _resolver() -> TieredPermissionResolver:
    return TieredPermissionResolver(config=PermissionConfig())


def test_build_launch_argv_does_not_project_opencode_reference_items_as_file_flags() -> None:
    adapter = OpenCodeAdapter()
    file_ref = ReferenceItem(
        kind="file",
        path=Path("/repo/src/main.py"),
        body="print('ok')",
    )
    run_inputs = ResolvedRunInputs(
        prompt="do thing",
        model=ModelId("opencode-gpt-5.4"),
        reference_items=(file_ref,),
    )
    perms = _resolver()
    projected_spec = resolve_launch_spec_stage(
        adapter=adapter,
        run_inputs=run_inputs,
        perms=perms,
    )
    assert isinstance(projected_spec, OpenCodeLaunchSpec)

    argv = build_launch_argv(
        adapter=adapter,
        run_inputs=run_inputs,
        perms=perms,
        projected_spec=projected_spec,
    )

    assert "--file" not in argv
    assert file_ref.path.as_posix() not in argv
    assert argv[-1] == "-"


def test_build_launch_argv_projects_claude_prompt_file_path_from_resolved_spec() -> None:
    adapter = ClaudeAdapter()
    prompt_file_path = "/tmp/.meridian/spawns/p123/prompt.md"
    projected_spec = ClaudeLaunchSpec(
        prompt="do thing",
        permission_resolver=_resolver(),
        appended_system_prompt="injected system prompt",
        prompt_file_path=prompt_file_path,
    )

    argv = build_launch_argv(
        adapter=adapter,
        run_inputs=ResolvedRunInputs(prompt="ignored by projection"),
        perms=_resolver(),
        projected_spec=projected_spec,
    )

    assert "--append-system-prompt-file" in argv
    flag_index = argv.index("--append-system-prompt-file")
    assert argv[flag_index + 1] == prompt_file_path
    assert "--append-system-prompt" not in argv


def test_build_launch_argv_routes_claude_subprocess_base_command_through_projection_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = ClaudeAdapter()
    projected_spec = ClaudeLaunchSpec(
        prompt="do thing",
        permission_resolver=_resolver(),
        interactive=False,
    )
    captured: dict[str, object] = {}

    def fake_project_subprocess_spec(
        harness_id: HarnessId,
        spec: object,
        *,
        base_command: tuple[str, ...],
    ) -> list[str]:
        captured["harness_id"] = harness_id
        captured["spec"] = spec
        captured["base_command"] = base_command
        return [*base_command, "projected"]

    monkeypatch.setattr(
        "meridian.lib.launch.command.project_subprocess_spec",
        fake_project_subprocess_spec,
    )

    argv = build_launch_argv(
        adapter=adapter,
        run_inputs=ResolvedRunInputs(prompt="ignored by projection"),
        perms=_resolver(),
        projected_spec=projected_spec,
    )

    assert captured["harness_id"] is HarnessId.CLAUDE
    assert captured["spec"] is projected_spec
    assert captured["base_command"] == adapter.BASE_COMMAND
    assert argv == (*adapter.BASE_COMMAND, "projected")


def test_build_launch_argv_ignores_removed_meridian_harness_command_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "MERIDIAN_HARNESS_COMMAND",
        "definitely-not-a-real-harness-binary",
    )
    adapter = ClaudeAdapter()
    run_inputs = ResolvedRunInputs(prompt="do thing")
    perms = _resolver()
    projected_spec = resolve_launch_spec_stage(
        adapter=adapter,
        run_inputs=run_inputs,
        perms=perms,
    )

    argv = build_launch_argv(
        adapter=adapter,
        run_inputs=run_inputs,
        perms=perms,
        projected_spec=projected_spec,
    )

    assert argv[: len(adapter.BASE_COMMAND)] == adapter.BASE_COMMAND
