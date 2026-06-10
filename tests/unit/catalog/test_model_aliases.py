from __future__ import annotations

import pytest

from meridian.lib.catalog.model_aliases import AliasEntry
from meridian.lib.core.types import HarnessId, ModelId


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
