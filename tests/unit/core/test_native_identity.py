"""Native keys preserve partial identity without fabricating complete bindings."""

import pytest

from meridian.lib.core.native_identity import (
    NativeEntryMismatch,
    NativeKey,
    NativeKeyFields,
    NativeSessionUnavailable,
)


def test_partial_fields_normalize_empty_and_complete_without_mutation() -> None:
    partial = NativeKeyFields("pi", "/store", "")
    assert partial.render() == {"harness": "pi", "native_store": "/store", "session_id": None}
    assert partial.complete() is None
    complete = partial.with_session("id")
    assert complete.complete() == NativeKey("pi", "/store", "id")
    assert NativeKey("pi", "/store", "id").fields() == complete
    assert partial.session_id is None
    assert NativeKeyFields("", "", "").render() == dict.fromkeys(
        ("harness", "native_store", "session_id")
    )


@pytest.mark.parametrize(
    "reason,code",
    [
        ("unbound", "unbound"),
        ("missing", "native_transcript_missing"),
        ("ambiguous_native_file", "ambiguous_native_file"),
    ],
)
def test_unavailable_reref_preserves_failure_reason(reason, code) -> None:
    original = NativeSessionUnavailable("native-id", reason)
    reref = original.for_ref("c42")
    assert reref.failure_code == original.failure_code == code
    assert reref.lifecycle_fields() == {"ref": "c42", "reason": reason}
    assert "c42" in str(reref) and "native-id" not in str(reref)
    assert original.ref == "native-id"


def test_mismatch_has_key_fields_and_separate_path_detail() -> None:
    key = NativeKeyFields("pi", "/store", "child")
    error = NativeEntryMismatch(key, key, "fork_parent", "expected parent /a, observed /b")
    assert error.expected == error.observed == key
    assert error.lifecycle_fields() == {
        "expected": key.render(),
        "observed": key.render(),
        "reason": "fork_parent",
        "detail": "expected parent /a, observed /b",
    }
    assert "fork_parent" in str(error) and str(key.render()) in str(error)
