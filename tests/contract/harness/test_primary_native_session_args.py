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

    with pytest.raises(ValueError):
        adapter.normalize_primary_session_args(("--resume", "session-id"), surface)


@pytest.mark.parametrize("surface", SURFACES)
def test_pi_adapter_retains_only_bounded_primary_overrides(surface: NativeSessionSurface) -> None:
    adapter = HarnessRegistry.with_defaults().get(HarnessId("pi"))
    args = ("--api-key", "secret", "--model=gpt-5.5", "-m", "gpt-5.4")
    normalized = adapter.normalize_primary_session_args(args, surface)
    assert normalized.selector is None
    assert normalized.remaining_args == args

    private_value = "SYNTHETIC_PRIVATE_PROFILE_VALUE"
    with pytest.raises(ValueError, match="not in the bounded primary option set") as error:
        adapter.normalize_primary_session_args(("--profile", private_value), surface)
    assert "--profile" not in str(error.value)
    assert private_value not in str(error.value)
