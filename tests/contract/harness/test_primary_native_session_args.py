"""Default primary native-session argument hook contract."""

from __future__ import annotations

import pytest

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.native_session_args import NativeSessionSurface
from meridian.lib.harness.registry import HarnessRegistry

SURFACES: tuple[NativeSessionSurface, ...] = ("subprocess", "managed")


@pytest.mark.parametrize("harness_id", HarnessRegistry.with_defaults().ids())
@pytest.mark.parametrize("surface", SURFACES)
def test_registered_adapters_refuse_unhandled_primary_native_session_args(
    harness_id: HarnessId, surface: NativeSessionSurface,
) -> None:
    adapter = HarnessRegistry.with_defaults().get(harness_id)

    absent = adapter.normalize_primary_session_args((), surface)
    assert absent.selector is None
    assert absent.remaining_args == ()

    with pytest.raises(ValueError, match="raw native session arguments are unsupported"):
        adapter.normalize_primary_session_args(("--resume", "session-id"), surface)
