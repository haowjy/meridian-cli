from __future__ import annotations

import pytest
from pydantic import ValidationError

from meridian.lib.catalog.model_aliases import AliasEntry, RunnablePath
from meridian.lib.catalog.model_policy import pattern_fallback_harness
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


def test_alias_entry_harness_model_id_for_returns_matching_harness_path() -> None:
    alias_entry = AliasEntry(
        alias="fast",
        model_id=ModelId("fake-model"),
        runnable_paths=(
            RunnablePath(harness="codex", harness_model_id="provider/codex-model"),
            RunnablePath(harness="opencode", harness_model_id="provider/opencode-model"),
        ),
    )

    assert alias_entry.harness_model_id_for("opencode") == "provider/opencode-model"


def test_alias_entry_harness_model_id_for_returns_none_when_path_does_not_match() -> None:
    alias_entry = AliasEntry(
        alias="fast",
        model_id=ModelId("fake-model"),
        runnable_paths=(
            RunnablePath(harness="codex", harness_model_id="provider/codex-model"),
        ),
    )

    assert alias_entry.harness_model_id_for("opencode") is None


def test_alias_entry_harness_model_id_for_returns_none_with_empty_paths() -> None:
    alias_entry = AliasEntry(alias="fast", model_id=ModelId("fake-model"))

    assert alias_entry.harness_model_id_for("opencode") is None


def test_alias_entry_harness_property_still_uses_resolved_or_pattern_fallback() -> None:
    resolved_entry = AliasEntry(
        alias="fast",
        model_id=ModelId("fake-model"),
        resolved_harness=HarnessId.OPENCODE,
    )
    fallback_entry = AliasEntry(
        alias="fast",
        model_id=ModelId("gpt-fallback-model"),
    )

    assert resolved_entry.harness == HarnessId.OPENCODE
    assert fallback_entry.harness == pattern_fallback_harness("gpt-fallback-model")
