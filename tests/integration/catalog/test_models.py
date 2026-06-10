from pathlib import Path

import pytest

from meridian.lib.core.types import HarnessId
from meridian.lib.ops.catalog import ModelsListInput, models_list_sync


def _model(
    model_id: str,
    *,
    provider: str = "openai",
    harness: HarnessId = HarnessId.CODEX,
    cost_input: float | None = 1.0,
    release_date: str | None = None,
    matched_aliases: list[str] | None = None,
    pinned: bool = False,
) -> dict[str, object]:
    return {
        "id": model_id,
        "name": model_id,
        "family": model_id.split("-", 1)[0],
        "provider": provider,
        "harness": str(harness),
        "cost_input": cost_input,
        "cost_output": cost_input,
        "context_limit": 200000,
        "output_limit": 8000,
        "capabilities": ["tool_call"],
        "release_date": release_date,
        "matched_aliases": matched_aliases or [],
        "pinned": pinned,
    }


def _init_repo(project_root: Path) -> None:
    project_root.mkdir()
    (project_root / "mars.toml").write_text(
        '[settings]\ntargets = [".claude"]\n',
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "list_input",
    [
        ModelsListInput(project_root=""),
        ModelsListInput(project_root="", all=True),
        ModelsListInput(project_root="", show_superseded=True),
    ],
    ids=["default", "all", "show-superseded"],
)
def test_models_list_passes_mars_rows_through_without_meridian_filters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    list_input: ModelsListInput,
) -> None:
    project_root = tmp_path / "repo"
    _init_repo(project_root)
    list_input = list_input.model_copy(update={"project_root": project_root.as_posix()})

    monkeypatch.setattr(
        "meridian.lib.ops.catalog.run_mars_models_list_all",
        lambda project_root=None: [
            _model("gpt-5.4", release_date="2026-05-01", matched_aliases=["gpt", "latest"]),
            _model("gpt-5.2", release_date="2026-04-01", matched_aliases=["stable"]),
            _model(
                "gemini-3.1-pro",
                provider="google",
                harness=HarnessId.OPENCODE,
                matched_aliases=["gem"],
            ),
            _model(
                "claude-expensive",
                provider="anthropic",
                harness=HarnessId.CLAUDE,
                cost_input=12.0,
            ),
            _model(
                "claude-old",
                provider="anthropic",
                harness=HarnessId.CLAUDE,
                release_date="2020-01-01",
            ),
            {
                "id": "unclaimed-model",
                "harness": None,
                "provider": "openai",
                "matched_aliases": ["unclaimed"],
                "description": "No harness installed.",
            },
        ],
    )

    output = models_list_sync(list_input)
    model_ids = [str(model.model_id) for model in output.models]
    assert model_ids == [
        "gpt-5.4",
        "gpt-5.2",
        "gemini-3.1-pro",
        "claude-expensive",
        "claude-old",
        "unclaimed-model",
    ]
    assert [alias.alias for alias in output.models[0].aliases] == ["gpt", "latest"]
    model = output.models[-1]
    assert model.harness is None
    assert model.to_wire()["harness"] is None
    assert "—" in output.format_text()


def test_models_list_default_path_delegates_to_mars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    _init_repo(project_root)

    monkeypatch.setattr(
        "meridian.lib.ops.catalog.run_mars_models_list_all",
        lambda project_root=None: [_model("gpt-5.4")],
    )

    output = models_list_sync(ModelsListInput(project_root=project_root.as_posix()))
    assert [str(model.model_id) for model in output.models] == ["gpt-5.4"]
