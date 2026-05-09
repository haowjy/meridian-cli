"""Unit coverage for catalog model resolution, superseding policy, and AgentModelEntry."""

import pytest
from pydantic import ValidationError

from meridian.lib.catalog.agent import AgentModelEntry
from meridian.lib.catalog.model_policy import (
    _model_lineage,
    compute_superseded_ids,
    pattern_fallback_harness,
)
from meridian.lib.catalog.models import resolve_model
from meridian.lib.core.types import HarnessId


def _mock_mars_list_all(project_root: object = None) -> list[dict[str, object]]:
    _ = project_root
    return [
        {"id": "gpt-5.4", "harness": "codex", "description": "GPT-5.4"},
        {"id": "gpt-5.4-mini", "harness": "codex", "description": "GPT-5.4 mini"},
    ]


def test_resolve_model_returns_concrete_model_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def mock_mars_resolve(
        name: str, project_root: object = None
    ) -> dict[str, object] | None:
        if name == "codex":
            return {
                "name": "codex",
                "model_id": "gpt-5.3-codex",
                "harness": "codex",
                "harness_source": "auto_detected",
            }
        return None

    monkeypatch.setattr(
        "meridian.lib.catalog.models.run_mars_models_resolve",
        mock_mars_resolve,
    )
    result = resolve_model("codex")
    assert str(result.model_id) == "gpt-5.3-codex"
    assert result.alias == "codex"
    assert result.harness == HarnessId.CODEX
    assert result.default_effort is None
    assert result.default_autocompact is None


def test_resolve_model_copies_alias_defaults_from_mars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def mock_mars_resolve(
        name: str, project_root: object = None
    ) -> dict[str, object] | None:
        if name == "gpt":
            return {
                "name": "gpt",
                "model_id": "gpt-5.5",
                "harness": "codex",
                "default_effort": "low",
                "autocompact": 65,
            }
        return None

    monkeypatch.setattr(
        "meridian.lib.catalog.models.run_mars_models_resolve",
        mock_mars_resolve,
    )
    result = resolve_model("gpt")
    assert str(result.model_id) == "gpt-5.5"
    assert result.alias == "gpt"
    assert result.default_effort == "low"
    assert result.default_autocompact == 65


def test_resolve_model_exact_full_model_id_beats_mars_prefix_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "meridian.lib.catalog.models.run_mars_models_list_all",
        _mock_mars_list_all,
    )

    def mock_mars_resolve(
        name: str, project_root: object = None
    ) -> dict[str, object] | None:
        if name == "gpt-5.4":
            return {
                "name": "gpt-5.4",
                "model_id": "gpt-5.4-mini",
                "harness": "codex",
                "source": "alias_prefix",
            }
        return None

    monkeypatch.setattr(
        "meridian.lib.catalog.models.run_mars_models_resolve",
        mock_mars_resolve,
    )

    result = resolve_model("gpt-5.4")

    assert str(result.model_id) == "gpt-5.4"
    assert result.alias == ""
    assert result.harness == HarnessId.CODEX
    assert result.resolved_harness == HarnessId.CODEX
    assert result.mars_provided_harness == HarnessId.CODEX


def test_resolve_model_skips_exact_id_guard_when_mars_cannot_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    list_all_calls = 0

    def mock_mars_resolve(
        name: str, project_root: object = None
    ) -> dict[str, object] | None:
        _ = name, project_root
        return None

    def fail_if_called(project_root: object = None) -> list[dict[str, object]]:
        nonlocal list_all_calls
        _ = project_root
        list_all_calls += 1
        return [{"id": "gpt-5.4", "harness": "codex"}]

    monkeypatch.setattr(
        "meridian.lib.catalog.models.run_mars_models_resolve",
        mock_mars_resolve,
    )
    monkeypatch.setattr(
        "meridian.lib.catalog.models.run_mars_models_list_all",
        fail_if_called,
    )

    result = resolve_model("gpt-5.4")

    assert str(result.model_id) == "gpt-5.4"
    assert list_all_calls == 0


def test_resolve_model_exact_full_model_id_preserves_mars_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "meridian.lib.catalog.models.run_mars_models_list_all",
        _mock_mars_list_all,
    )

    def mock_mars_resolve(
        name: str, project_root: object = None
    ) -> dict[str, object] | None:
        if name == "gpt-5.4":
            return {
                "name": "gpt-5.4",
                "model_id": "gpt-5.4",
                "harness": "codex",
                "default_effort": "medium",
                "autocompact": 70,
            }
        return None

    monkeypatch.setattr(
        "meridian.lib.catalog.models.run_mars_models_resolve",
        mock_mars_resolve,
    )

    result = resolve_model("gpt-5.4")

    assert str(result.model_id) == "gpt-5.4"
    assert result.alias == "gpt-5.4"
    assert result.harness == HarnessId.CODEX
    assert result.default_effort == "medium"
    assert result.default_autocompact == 70


def test_resolve_model_exact_id_same_resolution_skips_exact_id_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    list_all_calls = 0

    def mock_mars_resolve(
        name: str, project_root: object = None
    ) -> dict[str, object] | None:
        _ = project_root
        if name == "gpt-5.4":
            return {
                "name": "gpt-5.4",
                "model_id": "gpt-5.4",
                "harness": "codex",
            }
        return None

    def track_list_all(project_root: object = None) -> list[dict[str, object]]:
        nonlocal list_all_calls
        _ = project_root
        list_all_calls += 1
        return [{"id": "gpt-5.4", "harness": "codex"}]

    monkeypatch.setattr(
        "meridian.lib.catalog.models.run_mars_models_resolve",
        mock_mars_resolve,
    )
    monkeypatch.setattr(
        "meridian.lib.catalog.models.run_mars_models_list_all",
        track_list_all,
    )

    result = resolve_model("gpt-5.4")

    assert str(result.model_id) == "gpt-5.4"
    assert list_all_calls == 0


def test_resolve_model_exact_full_model_id_without_mars_harness_keeps_raw_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def list_models(project_root: object = None) -> list[dict[str, object]]:
        _ = project_root
        return [{"id": "gpt-5.4", "harness": None, "description": "GPT-5.4"}]

    monkeypatch.setattr(
        "meridian.lib.catalog.models.run_mars_models_list_all",
        list_models,
    )

    def mock_mars_resolve(
        name: str, project_root: object = None
    ) -> dict[str, object] | None:
        if name == "gpt-5.4":
            return {
                "name": "gpt-5.4",
                "model_id": "gpt-5.4-mini",
                "harness": "codex",
                "source": "alias_prefix",
            }
        return None

    monkeypatch.setattr(
        "meridian.lib.catalog.models.run_mars_models_resolve",
        mock_mars_resolve,
    )

    result = resolve_model("gpt-5.4")

    assert str(result.model_id) == "gpt-5.4"
    assert result.alias == ""
    assert result.harness == HarnessId.CODEX
    assert result.resolved_harness is None
    assert result.mars_provided_harness is None


def test_resolve_model_keeps_unavailable_explicit_harness_from_mars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def mock_mars_resolve(
        name: str, project_root: object = None
    ) -> dict[str, object] | None:
        if name == "sonnet":
            return {
                "name": "sonnet",
                "model_id": "claude-sonnet-4-6",
                "harness": "claude",
                "harness_source": "unavailable",
                "harness_candidates": ["claude", "opencode", "gemini"],
            }
        return None

    monkeypatch.setattr(
        "meridian.lib.catalog.models.run_mars_models_resolve",
        mock_mars_resolve,
    )

    result = resolve_model("sonnet")

    assert str(result.model_id) == "claude-sonnet-4-6"
    assert result.harness == HarnessId.CLAUDE


def test_resolve_model_raw_model_id_pattern_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def mock_mars_resolve(
        name: str, project_root: object = None
    ) -> dict[str, object] | None:
        return None

    monkeypatch.setattr(
        "meridian.lib.catalog.models.run_mars_models_resolve",
        mock_mars_resolve,
    )
    result = resolve_model("claude-opus-4-6")
    assert str(result.model_id) == "claude-opus-4-6"
    assert result.alias == ""
    assert result.harness == HarnessId.CLAUDE
    assert result.resolved_harness is None
    assert result.mars_provided_harness is None
    assert result.default_effort is None
    assert result.default_autocompact is None


def test_resolve_model_unknown_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def mock_mars_resolve(
        name: str, project_root: object = None
    ) -> dict[str, object] | None:
        return None

    monkeypatch.setattr(
        "meridian.lib.catalog.models.run_mars_models_resolve",
        mock_mars_resolve,
    )
    result = resolve_model("some-unknown-model")
    assert str(result.model_id) == "some-unknown-model"
    assert result.resolved_harness is None
    with pytest.raises(ValueError, match="Unknown model"):
        _ = result.harness


def test_resolve_model_unavailable_harness_without_explicit_route_uses_pattern_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def mock_mars_resolve(
        name: str, project_root: object = None
    ) -> dict[str, object] | None:
        return {
            "name": "opus",
            "model_id": "claude-opus-4-6",
            "harness": None,
            "harness_source": "unavailable",
            "harness_candidates": ["claude", "opencode"],
        }

    monkeypatch.setattr(
        "meridian.lib.catalog.models.run_mars_models_resolve",
        mock_mars_resolve,
    )

    result = resolve_model("opus")

    assert str(result.model_id) == "claude-opus-4-6"
    assert result.harness == HarnessId.CLAUDE
    assert result.resolved_harness is None
    assert result.mars_provided_harness is None


def test_resolve_model_empty_raises() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        resolve_model("")
    with pytest.raises(ValueError, match="must not be empty"):
        resolve_model("   ")


def test_pattern_fallback_harness() -> None:
    assert pattern_fallback_harness("claude-opus-4-6") == HarnessId.CLAUDE
    assert pattern_fallback_harness("gpt-5.3-codex") == HarnessId.CODEX
    assert pattern_fallback_harness("gemini-pro") == HarnessId.OPENCODE

    with pytest.raises(ValueError):
        pattern_fallback_harness("totally-unknown-model")


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("claude-opus-4-6", "claude-opus"),
        ("claude-3-5-sonnet-20241022", "claude-sonnet"),
        ("gpt-5.4-mini", "gpt-mini"),
        ("gpt-5.3-codex", "gpt-codex"),
        ("gemini-2.5-flash-lite-preview-06-17", "gemini-flash-lite"),
        ("claude-sonnet-latest", None),
    ],
)
def test_model_lineage_normalizes_version_tokens(model_id: str, expected: str | None) -> None:
    assert _model_lineage(model_id) == expected


def test_compute_superseded_ids_respects_lineage_and_provider_boundaries() -> None:
    superseded = compute_superseded_ids(
        [
            ("gpt-5.1", "openai", "2025-11-13"),
            ("gpt-5.2", "openai", "2025-12-11"),
            ("gpt-5.4", "openai", "2026-03-05"),
            ("gpt-5.3-codex", "openai", "2026-02-05"),
            ("gemini-pro", "google", "2025-01-01"),
            ("gemini-pro", "other", "2026-01-01"),
        ]
    )

    assert superseded == frozenset({"gpt-5.1", "gpt-5.2"})


# --- AgentModelEntry validation ---


class TestAgentModelEntry:
    def test_model_entry_normalizes_effort_and_keeps_supported_fields(self) -> None:
        entry = AgentModelEntry.model_validate(
            {
                "effort": "  high  ",
                "autocompact": 200000,
                "lane": "correctness",
            }
        )

        assert entry.effort == "high"
        assert entry.autocompact == 200000

    @pytest.mark.parametrize("effort", ["auto", "ultra"])
    def test_invalid_effort_raises(self, effort: str) -> None:
        with pytest.raises(ValidationError, match="expected one of"):
            AgentModelEntry(effort=effort)

    @pytest.mark.parametrize("value", [1000, 200000])
    def test_autocompact_accepts_supported_bounds(self, value: int) -> None:
        assert AgentModelEntry(autocompact=value).autocompact == value

    def test_autocompact_bool_raises(self) -> None:
        with pytest.raises(ValidationError, match="boolean"):
            AgentModelEntry(autocompact=True)  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", [0, 999])
    def test_autocompact_out_of_range_raises(self, value: int) -> None:
        with pytest.raises(ValidationError, match="autocompact"):
            AgentModelEntry(autocompact=value)

    @pytest.mark.parametrize("value", [1, 100])
    def test_autocompact_pct_accepts_supported_bounds(self, value: int) -> None:
        assert AgentModelEntry(autocompact_pct=value).autocompact_pct == value

    def test_autocompact_pct_bool_raises(self) -> None:
        with pytest.raises(ValidationError, match="boolean"):
            AgentModelEntry(autocompact_pct=True)  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", [0, 101])
    def test_autocompact_pct_out_of_range_raises(self, value: int) -> None:
        with pytest.raises(ValidationError, match="autocompact"):
            AgentModelEntry(autocompact_pct=value)

    def test_autocompact_pct_stored_independently_from_autocompact(self) -> None:
        entry = AgentModelEntry(autocompact=50000, autocompact_pct=75)
        assert entry.autocompact == 50000
        assert entry.autocompact_pct == 75
