"""Per-adapter SpawnParams accounting guard."""

from __future__ import annotations

import pytest

from meridian.lib.harness.bundle import get_bundle_registry
from meridian.lib.harness.claude import ClaudeAdapter
from meridian.lib.harness.launch_spec import _enforce_spawn_params_accounting


def test_accounting_rejects_field_missing_from_one_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ClaudeAdapter,
        "_EXPLICITLY_IGNORED_FIELDS",
        ClaudeAdapter._EXPLICITLY_IGNORED_FIELDS - {"model_override_explicit"},
    )

    with pytest.raises(
        ImportError,
        match=r"claude: missing=\['model_override_explicit'\], stale=\[\]",
    ):
        _enforce_spawn_params_accounting(get_bundle_registry())
