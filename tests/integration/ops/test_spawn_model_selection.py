from pathlib import Path

import pytest

import meridian.lib.ops.spawn.api as spawn_api
from meridian.lib.catalog.model_aliases import AliasEntry
from meridian.lib.core.types import HarnessId, ModelId
from meridian.lib.ops.spawn.models import SpawnCreateInput
from tests.support.fixtures import write_agent


def _write_minimal_mars_config(project_root: Path) -> None:
    (project_root / "mars.toml").write_text(
        "[settings]\n"
        'targets = [".claude"]\n',
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _isolate_runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MERIDIAN_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")


def test_spawn_create_dry_run_threads_model_selection_through_prepare_and_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    _write_minimal_mars_config(project_root)
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

    prepare_calls: list[str] = []
    policy_calls: list[str] = []

    def prepare_resolve_model(name: str, project_root: Path | None = None) -> AliasEntry:
        _ = project_root
        prepare_calls.append(name)
        return {"gpt55": alias, "gpt-5.5": canonical}[name]

    def policy_resolve_model(
        name: str,
        project_root: Path | None = None,
        cache: object | None = None,
    ) -> AliasEntry:
        _ = (project_root, cache)
        policy_calls.append(name)
        return {"gpt55": alias, "gpt-5.5": canonical}[name]

    monkeypatch.setattr(
        "meridian.lib.ops.spawn.prepare.resolve_model",
        prepare_resolve_model,
    )
    monkeypatch.setattr(
        "meridian.lib.launch.policies.resolve_model_entry",
        policy_resolve_model,
    )
    monkeypatch.setattr(
        "meridian.lib.launch.policies.load_merged_aliases",
        lambda project_root=None, cache=None: [alias, canonical],
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
    assert prepare_calls == []
    assert policy_calls == ["gpt55"]

    assert result.to_wire()["model_selection"] == {
        "requested_token": "gpt55",
        "canonical_model_id": "gpt-5.5",
        "harness_provenance": "mars-provided",
    }
    assert "Routing: mars-provided" in result.format_text()
