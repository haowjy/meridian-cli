from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meridian.lib.core.types import HarnessId
from meridian.lib.launch import bundle_adapter
from meridian.lib.launch.bundle_adapter import LoadedSkillEntry
from meridian.lib.launch.composition import AvailableSkillEntry
from meridian.lib.launch.launch_types import ResolvedExecutionPolicy


@dataclass(frozen=True)
class FakeBundleResult:
    model: str
    model_token: str
    harness: HarnessId
    harness_model: str | None
    execution_policy: ResolvedExecutionPolicy
    provenance: dict[str, str]
    warnings: tuple[str, ...] = ()
    prompt_surface_inventory_prompt: str = ""
    tools_allowed: tuple[str, ...] = ()
    tools_disallowed: tuple[str, ...] = ()
    tools_mcp: tuple[str, ...] = ()
    skills_loaded: tuple[LoadedSkillEntry, ...] = ()
    skills_available: tuple[AvailableSkillEntry, ...] = ()
    skills_missing: tuple[str, ...] = ()


def stub_bundle_request_and_resolve(
    monkeypatch: Any,
    *,
    model: str,
    harness: HarnessId,
    model_token: str | None = None,
    harness_model: str | None = None,
    execution_policy: ResolvedExecutionPolicy | None = None,
    provenance: dict[str, str] | None = None,
    warnings: tuple[str, ...] = (),
    prompt_surface_inventory_prompt: str = "",
    tools_allowed: tuple[str, ...] = (),
    tools_disallowed: tuple[str, ...] = (),
    tools_mcp: tuple[str, ...] = (),
    skills_loaded: tuple[LoadedSkillEntry, ...] = (),
) -> list[bundle_adapter.BundleRequest]:
    captured_requests: list[bundle_adapter.BundleRequest] = []

    def _fake_request_and_resolve(
        request: bundle_adapter.BundleRequest,
        *,
        harness_registry: object,
    ) -> FakeBundleResult:
        _ = harness_registry
        captured_requests.append(request)
        resolved_policy = execution_policy or ResolvedExecutionPolicy()
        resolved_provenance = provenance or {"model_source": "cli", "harness_source": "provider"}
        resolved_model_token = (
            model_token if model_token is not None else request.model_override or model
        )
        resolved_harness_model = harness_model if harness_model is not None else model
        return FakeBundleResult(
            model=model,
            model_token=resolved_model_token,
            harness=harness,
            harness_model=resolved_harness_model,
            execution_policy=resolved_policy,
            provenance=resolved_provenance,
            warnings=warnings,
            prompt_surface_inventory_prompt=prompt_surface_inventory_prompt,
            tools_allowed=tools_allowed,
            tools_disallowed=tools_disallowed,
            tools_mcp=tools_mcp,
            skills_loaded=skills_loaded,
        )

    monkeypatch.setattr(bundle_adapter, "request_and_resolve", _fake_request_and_resolve)
    return captured_requests


def assert_task_cwd_instruction(system_prompt: str, task_dir: Path | str) -> None:
    """Assert the harness-aware task-dir guidance is present and unambiguous."""
    task_dir_posix = Path(task_dir).as_posix()
    assert "# Source-edit directory" in system_prompt
    assert "Use `MERIDIAN_PROJECT_ROOT` for project coordination files" in system_prompt
    assert task_dir_posix in system_prompt
    assert "MERIDIAN_TASK_DIR" in system_prompt
    assert "project root, NOT" in system_prompt
    assert "relative paths resolve against the project root" in system_prompt
    assert "absolute paths" in system_prompt
    assert "`cd`" in system_prompt
    assert "Never assume cwd is the task dir" in system_prompt
