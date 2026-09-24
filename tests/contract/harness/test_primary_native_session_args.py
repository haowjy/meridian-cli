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

    if harness_id == HarnessId("claude") and surface == "managed":
        with pytest.raises(ValueError):
            adapter.normalize_primary_session_args((), surface)
        with pytest.raises(ValueError):
            adapter.normalize_primary_session_args(("--resume", "session-id"), surface)
        return

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


def test_registered_claude_adapter_normalizes_native_resume() -> None:
    adapter = HarnessRegistry.with_defaults().get(HarnessId("claude"))
    native_id = "123e4567-e89b-12d3-a456-426614174000"

    normalized = adapter.normalize_primary_session_args(
        ("--resume", native_id, "--model", "claude-sonnet"), "subprocess"
    )

    assert (normalized.selector.operation, normalized.selector.native_id) == (
        "resume",
        native_id,
    )
    assert normalized.remaining_args == ("--model", "claude-sonnet")
    with pytest.raises(ValueError):
        adapter.normalize_primary_session_args(("--resume", "not-a-uuid"), "subprocess")
    with pytest.raises(ValueError):
        adapter.normalize_primary_session_args(("--resume", native_id), "managed")


def test_registered_opencode_adapter_normalizes_native_resume_and_logging() -> None:
    adapter = HarnessRegistry.with_defaults().get(HarnessId("opencode"))

    normalized = adapter.normalize_primary_session_args(
        ("--session", "ses_native_123", "--log-level", "INFO"), "managed"
    )

    assert (normalized.selector.operation, normalized.selector.native_id) == (
        "resume",
        "ses_native_123",
    )
    assert normalized.remaining_args == ("--log-level", "INFO")
    with pytest.raises(ValueError):
        adapter.normalize_primary_session_args(("--session", "session-id"), "subprocess")
    with pytest.raises(ValueError):
        adapter.normalize_primary_session_args(
            ("--session", "ses_native_123", "--model", "x"), "managed"
        )
    with pytest.raises(ValueError):
        adapter.normalize_primary_session_args((), "unknown")  # type: ignore[arg-type]


def test_registered_default_adapter_stays_fail_closed() -> None:
    adapter = HarnessRegistry.with_defaults().get(HarnessId("cursor"))
    with pytest.raises(ValueError):
        adapter.normalize_primary_session_args(("--resume", "session-id"), "subprocess")
