# qa-validated: test-suite-redesign
from pathlib import Path

import pytest

import meridian.lib.ops.spawn.api as spawn_api
from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.catalog.model_aliases import AliasEntry
from meridian.lib.core.types import HarnessId, ModelId
from meridian.lib.ops.spawn.models import SpawnCreateInput
from tests.support.fixtures import write_agent, write_minimal_mars_config


def test_spawn_create_dry_run_threads_model_selection_through_prepare_and_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    write_minimal_mars_config(project_root)
    write_agent(project_root, name="reviewer", model="gpt55")

    alias = AliasEntry(
        alias="gpt55",
        model_id=ModelId("gpt-5.5"),
        resolved_harness=HarnessId.CODEX,
    )
    canonical = AliasEntry(
        alias="",
        model_id=ModelId("gpt-5.5"),
        resolved_harness=HarnessId.CODEX,
    )

    policy_calls: list[str] = []

    def policy_resolve_model(self: CatalogSession, name: str) -> AliasEntry:
        policy_calls.append(name)
        return {"gpt55": alias, "gpt-5.5": canonical}[name]

    monkeypatch.setattr(
        CatalogSession,
        "resolve_model",
        policy_resolve_model,
    )
    monkeypatch.setattr(
        CatalogSession,
        "load_aliases",
        lambda self: [alias, canonical],
    )

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
    assert result.harness_id == "codex"
    assert policy_calls == ["gpt55"]

    assert result.to_wire()["model_selection"] == {
        "requested_token": "gpt55",
        "canonical_model_id": "gpt-5.5",
        "harness_provenance": "mars-provided",
    }
    assert "Routing: mars-provided" in result.format_text()
