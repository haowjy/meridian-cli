from __future__ import annotations

import pytest
from pydantic import ValidationError

from meridian.lib.catalog.model_aliases import AliasEntry, RunnablePath
from meridian.lib.core.types import HarnessId, ModelId


def test_runnable_path_constructs_with_valid_data() -> None:
    path = RunnablePath(
        harness="opencode",
        harness_model_id="provider/opencode-model",
        provider="models-dev",
    )

    assert path.harness == "opencode"
    assert path.harness_model_id == "provider/opencode-model"
    assert path.provider == "models-dev"


def test_runnable_path_is_frozen() -> None:
    path = RunnablePath(harness="codex", harness_model_id="provider/codex-model")

    with pytest.raises(ValidationError, match="frozen"):
        path.harness = "opencode"


def test_runnable_path_provider_defaults_to_empty_string() -> None:
    path = RunnablePath(harness="codex", harness_model_id="provider/codex-model")

    assert path.provider == ""


def test_alias_entry_defaults_harness_candidates_and_runnable_paths_to_empty_tuples() -> None:
    alias_entry = AliasEntry(alias="fast", model_id=ModelId("fake-model"))

    assert alias_entry.harness_candidates == ()
    assert alias_entry.runnable_paths == ()


def test_alias_entry_harness_property_requires_mars_resolved_harness() -> None:
    resolved_entry = AliasEntry(
        alias="fast",
        model_id=ModelId("fake-model"),
        resolved_harness=HarnessId.OPENCODE,
    )
    unresolved_entry = AliasEntry(
        alias="fast",
        model_id=ModelId("gpt-fallback-model"),
    )

    assert resolved_entry.harness == HarnessId.OPENCODE
    with pytest.raises(
        ValueError,
        match="Model harness is missing from Mars resolution",
    ):
        _ = unresolved_entry.harness
