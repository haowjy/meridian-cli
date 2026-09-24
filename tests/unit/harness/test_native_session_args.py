"""Adapter-local raw native-session argument normalization contract."""

import pytest

from meridian.lib.harness.native_session_args import (
    NativeSessionSelector,
    NativeSessionSurface,
    NormalizedNativeSessionArgs,
    normalize_native_session_args,
)


def test_unimplemented_adapter_accepts_only_absent_raw_input() -> None:
    assert normalize_native_session_args((), "subprocess", None) == NormalizedNativeSessionArgs(
        None, ()
    )
    with pytest.raises(ValueError, match="unsupported"):
        normalize_native_session_args(("--resume", "secret-id"), "managed", None)


def test_adapter_normalizer_dispatches_and_returns_result_unchanged() -> None:
    received: list[tuple[tuple[str, ...], NativeSessionSurface]] = []
    result = NormalizedNativeSessionArgs(NativeSessionSelector("resume", "native-A"), ("tail",))

    def normalize(
        args: tuple[str, ...], surface: NativeSessionSurface
    ) -> NormalizedNativeSessionArgs:
        received.append((args, surface))
        return result

    raw = ("adapter-owned", "argv")
    assert normalize_native_session_args(raw, "managed", normalize) is result
    assert received == [(raw, "managed")]


def test_adapter_normalizer_propagates_exception_unchanged() -> None:
    failure = ValueError("adapter rejected input")

    def normalize(
        args: tuple[str, ...], surface: NativeSessionSurface
    ) -> NormalizedNativeSessionArgs:
        _ = args, surface
        raise failure

    with pytest.raises(ValueError) as raised:
        normalize_native_session_args(("adapter-owned",), "subprocess", normalize)
    assert raised.value is failure


def test_selector_value_rejects_blank_native_ids() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        NativeSessionSelector("resume", " ")
