from datetime import date, timedelta
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


def test_models_list_uses_visibility_rules_and_keeps_aliased_models_visible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    _init_repo(project_root)

    old_date = (date.today() - timedelta(days=365)).isoformat()
    monkeypatch.setattr(
        "meridian.lib.ops.catalog.run_mars_models_list_all",
        lambda project_root=None: [
            _model("gpt-5.4"),
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
                release_date=old_date,
            ),
        ],
    )

    output = models_list_sync(ModelsListInput(project_root=project_root.as_posix()))
    model_ids = {str(model.model_id) for model in output.models}
    assert model_ids == {"gpt-5.4", "gemini-3.1-pro"}


@pytest.mark.parametrize(
    ("show_superseded", "expected_model_ids"),
    [
        (False, {"gpt-5.4"}),
        (True, {"gpt-5.4", "gpt-5.2"}),
    ],
)
def test_models_list_superseded_visibility_toggle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    show_superseded: bool,
    expected_model_ids: set[str],
) -> None:
    project_root = tmp_path / "repo"
    _init_repo(project_root)

    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=30)).isoformat()
    monkeypatch.setattr(
        "meridian.lib.ops.catalog.run_mars_models_list_all",
        lambda project_root=None: [
            _model("gpt-5.4", release_date=today),
            _model("gpt-5.2", release_date=yesterday),
        ],
    )

    output = models_list_sync(
        ModelsListInput(
            project_root=project_root.as_posix(),
            show_superseded=show_superseded,
        )
    )
    model_ids = {str(model.model_id) for model in output.models}
    assert model_ids == expected_model_ids


def test_models_list_aliased_model_survives_superseded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    _init_repo(project_root)

    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=30)).isoformat()
    monkeypatch.setattr(
        "meridian.lib.ops.catalog.run_mars_models_list_all",
        lambda project_root=None: [
            _model("gpt-5.4", release_date=today),
            _model("gpt-5.2", release_date=yesterday, matched_aliases=["old-gpt"]),
        ],
    )

    output = models_list_sync(ModelsListInput(project_root=project_root.as_posix()))
    model_ids = {str(model.model_id) for model in output.models}
    assert "gpt-5.4" in model_ids
    assert "gpt-5.2" in model_ids


def test_models_list_all_delegates_to_mars_without_meridian_filters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    _init_repo(project_root)

    monkeypatch.setattr(
        "meridian.lib.ops.catalog.run_mars_models_list_all",
        lambda project_root=None: [
            {
                "id": "gpt-5.4",
                "harness": "codex",
                "provider": "openai",
                "release_date": date.today().isoformat(),
                "matched_aliases": ["gpt", "latest"],
            },
            {
                "id": "gpt-5.2",
                "harness": "codex",
                "provider": "openai",
                "release_date": (date.today() - timedelta(days=30)).isoformat(),
                "matched_aliases": ["stable"],
            },
        ],
    )

    output = models_list_sync(ModelsListInput(project_root=project_root.as_posix(), all=True))
    model_ids = [str(model.model_id) for model in output.models]
    assert model_ids == ["gpt-5.4", "gpt-5.2"]
    assert [alias.alias for alias in output.models[0].aliases] == ["gpt", "latest"]


def test_models_list_all_preserves_null_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    _init_repo(project_root)

    monkeypatch.setattr(
        "meridian.lib.ops.catalog.run_mars_models_list_all",
        lambda project_root=None: [
            {
                "id": "gpt-5.4",
                "harness": None,
                "provider": "openai",
                "matched_aliases": ["gpt"],
                "description": "No harness installed.",
            },
        ],
    )

    output = models_list_sync(ModelsListInput(project_root=project_root.as_posix(), all=True))
    assert len(output.models) == 1
    model = output.models[0]
    assert str(model.model_id) == "gpt-5.4"
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
