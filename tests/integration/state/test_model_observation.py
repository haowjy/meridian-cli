"""Executed-model observation round-trip and dedupe, independent of intent."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.state import session_store as store


def _observation(harness: str, native: str, token: str) -> store.SessionModelObservationEvent:
    return store.SessionModelObservationEvent(
        harness=harness,
        harness_session_id=native,
        observed_model_token=token,
        recorded_at="2026-09-15T00:00:00Z",
    )


def test_observation_round_trip_and_dedupe(tmp_path: Path) -> None:
    root = tmp_path / ".meridian"
    root.mkdir(parents=True)

    assert store.get_last_executed_model(root, "codex", "thread-1") is None

    event = _observation("codex", "thread-1", "gpt-5.6-luna")
    assert store.record_model_observation(root, event) is True
    assert store.get_last_executed_model(root, "codex", "thread-1") == "gpt-5.6-luna"

    # Same identity and token dedupes without appending.
    assert store.record_model_observation(root, event) is False

    # A new token for the same identity appends and wins.
    newer = _observation("codex", "thread-1", "gpt-5.6-sol")
    assert store.record_model_observation(root, newer) is True
    assert store.get_last_executed_model(root, "codex", "thread-1") == "gpt-5.6-sol"

    # Identity is scoped by (harness, harness_session_id).
    assert store.get_last_executed_model(root, "codex", "thread-2") is None
    assert store.get_last_executed_model(root, "claude", "thread-1") is None


def test_observation_does_not_require_a_session_start(tmp_path: Path) -> None:
    root = tmp_path / ".meridian"
    root.mkdir(parents=True)

    assert (
        store.record_model_observation(root, _observation("opencode", "native-1", "deepseek/x"))
        is True
    )
    assert store.get_last_executed_model(root, "opencode", "native-1") == "deepseek/x"
    # The observation never projects a session record or mutates selection intent.
    assert store.list_all_session_records(root) == []
    assert store.get_model_selection(root, "opencode", "native-1") is None
