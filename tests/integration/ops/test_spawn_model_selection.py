# qa-validated: test-suite-redesign
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import meridian.lib.ops.spawn.api as spawn_api
from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.core.types import HarnessId
from meridian.lib.launch import bundle_adapter
from meridian.lib.launch.launch_types import ResolvedExecutionPolicy
from meridian.lib.ops.spawn.models import SpawnCreateInput
from tests.support.fixtures import write_agent, write_minimal_mars_config


@dataclass(frozen=True)
class _FakeBundleResult:
    model: str
    model_token: str
    harness: HarnessId
    harness_model: str | None
    execution_policy: ResolvedExecutionPolicy
    provenance: dict[str, str]
    warnings: tuple[str, ...] = ()
    prompt_surface_system_instruction: str = ""
    prompt_surface_supplemental_documents: tuple[object, ...] = ()
    prompt_surface_inventory_prompt: str = ""
    tools_allowed: tuple[str, ...] = ()
    tools_disallowed: tuple[str, ...] = ()
    tools_mcp: tuple[str, ...] = ()
    skills_loaded: tuple[str, ...] = ()
    skills_missing: tuple[str, ...] = ()


def test_spawn_create_dry_run_threads_bundle_model_selection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    write_minimal_mars_config(project_root)
    write_agent(project_root, name="reviewer", model="gpt55")

    monkeypatch.setattr(
        bundle_adapter,
        "request_and_resolve",
        lambda request, *, harness_registry: _FakeBundleResult(
            model="gpt-5.5",
            model_token="gpt55",
            harness=HarnessId.OPENCODE,
            harness_model="openai/gpt-5.5",
            execution_policy=ResolvedExecutionPolicy(),
            provenance={"model_source": "cli", "harness_source": "provider"},
        ),
    )
    monkeypatch.setattr(
        CatalogSession,
        "resolve_model",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("spawn routing must not call CatalogSession.resolve_model")
        ),
    )
    monkeypatch.setattr(CatalogSession, "alias_map", lambda self: {})

    result = spawn_api.spawn_create_sync(
        SpawnCreateInput(
            prompt="probe routing provenance",
            model="gpt55",
            agent="reviewer",
            project_root=project_root.as_posix(),
            dry_run=True,
        )
    )

    assert result.status == "dry-run"
    assert result.model == "gpt-5.5"
    assert result.harness_id == "opencode"
    assert result.to_wire()["model_selection"] == {
        "requested_token": "gpt55",
        "canonical_model_id": "gpt-5.5",
        "harness_provenance": "provider",
    }


def test_spawn_create_dry_run_bundle_path_has_no_compiler_fallback_chain(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    write_minimal_mars_config(project_root)
    write_agent(project_root, name="reviewer", model="gpt55")

    monkeypatch.setattr(
        bundle_adapter,
        "request_and_resolve",
        lambda request, *, harness_registry: _FakeBundleResult(
            model="gpt-5.5",
            model_token="gpt55",
            harness=HarnessId.CODEX,
            harness_model="gpt-5.5",
            execution_policy=ResolvedExecutionPolicy(effort="high"),
            provenance={"model_source": "profile-default", "harness_source": "provider"},
        ),
    )
    monkeypatch.setattr(CatalogSession, "alias_map", lambda self: {})

    result = spawn_api.spawn_create_sync(
        SpawnCreateInput(
            prompt="show fallback chain",
            agent="reviewer",
            project_root=project_root.as_posix(),
            dry_run=True,
        )
    )

    assert result.status == "dry-run"
    assert "fallback_chain" not in result.to_wire()
