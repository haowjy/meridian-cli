"""Adapter-local raw native-session argument normalization contract."""

import pytest

from meridian.lib.harness.native_session_args import (
    NativeSessionSelector,
    NormalizedNativeSessionArgs,
    normalize_native_session_args,
)


def test_unimplemented_adapter_accepts_only_absent_raw_input() -> None:
    assert normalize_native_session_args((), None) == NormalizedNativeSessionArgs(None, ())
    with pytest.raises(ValueError, match="unsupported"):
        normalize_native_session_args(("--resume", "secret-id"), None)


def test_adapter_normalizer_consumes_selector_and_preserves_remainder_exactly() -> None:
    calls = 0

    def normalize(args: tuple[str, ...]) -> NormalizedNativeSessionArgs:
        nonlocal calls
        calls += 1
        if args[:2] == ("--resume", "native-A"):
            return NormalizedNativeSessionArgs(
                NativeSessionSelector("resume", "native-A"), args[2:]
            )
        raise ValueError("unsupported raw selector")

    raw = ("--resume", "native-A", "--model=claude-opus-4", "--", "keep bytes")
    normalized = normalize_native_session_args(raw, normalize)

    assert normalized.selector == NativeSessionSelector("resume", "native-A")
    assert normalized.remaining_args == ("--model=claude-opus-4", "--", "keep bytes")
    assert calls == 1


def test_adapter_normalizer_refuses_duplicate_selector_vector() -> None:
    def normalize(args: tuple[str, ...]) -> NormalizedNativeSessionArgs:
        if args == ("--resume", "native-A"):
            return NormalizedNativeSessionArgs(NativeSessionSelector("resume", "native-A"), ())
        raise ValueError("duplicate or malformed selector")

    with pytest.raises(ValueError, match="duplicate"):
        normalize_native_session_args(("--resume", "native-A", "--resume", "native-A"), normalize)


def test_selector_value_rejects_blank_native_ids() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        NativeSessionSelector("resume", " ")
