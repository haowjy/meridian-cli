"""Strict normalization for native resume/fork sources."""

from pathlib import Path

from meridian.lib.ops.reference import (
    AuthorizedSourceUse,
    SourceUseRefused,
    UntrackedSourceUse,
    resolve_source_use,
)
from meridian.lib.state import session_store, spawn_store
from tests.integration.ops.test_native_reference_authority import _pinned_journal


def test_chat_reference_returns_exact_authorized_source_and_original_ref(tmp_path: Path) -> None:
    locator = _pinned_journal(tmp_path)

    result = resolve_source_use(tmp_path, "resume", " c1 ")

    assert isinstance(result, AuthorizedSourceUse)
    assert result.original_ref == " c1 "
    assert result.source.ref.chat_id == "c1"
    assert result.source.locator == locator
    assert result.source.key.native_session_id == "conversation"


def test_bare_id_requires_clean_unique_v4_claim_and_harness_match(tmp_path: Path) -> None:
    _pinned_journal(tmp_path)

    allowed = resolve_source_use(tmp_path, "fork", "conversation", "pi")
    mismatch = resolve_source_use(tmp_path, "resume", "conversation", "claude")

    assert isinstance(allowed, AuthorizedSourceUse)
    assert allowed.source.key.native_session_id == "conversation"
    assert mismatch == SourceUseRefused(
        "resume", "conversation", "harness_mismatch", "c1"
    )


def test_bare_id_refuses_legacy_contradiction_for_selected_chat(tmp_path: Path) -> None:
    _pinned_journal(tmp_path)
    session_store.start_session(tmp_path, "pi", "other-id", "test", chat_id="c1")

    result = resolve_source_use(tmp_path, "resume", "conversation", "pi")

    assert isinstance(result, SourceUseRefused)
    assert result.reason == "native_claim_blocked"


def test_invalid_authority_is_not_an_untracked_negative(tmp_path: Path) -> None:
    (tmp_path / "sessions.jsonl").write_bytes(b'{"event":"start"')

    result = resolve_source_use(tmp_path, "resume", "unknown-native", "pi")

    assert result == SourceUseRefused(
        "resume", "unknown-native", "native_claim_unavailable"
    )


def test_untracked_bare_id_requires_complete_negative_lookup(tmp_path: Path) -> None:
    result = resolve_source_use(tmp_path, "fork", "genuinely-new", "pi")

    assert isinstance(result, UntrackedSourceUse)
    assert result.native_id == "genuinely-new"
    assert result.harness == "pi"
    assert result.lookup_scope == tmp_path


def test_p_spawn_preserves_identity_and_only_untracked_negative_survives(tmp_path: Path) -> None:
    spawn_store.start_spawn(
        tmp_path,
        chat_id="c9",
        model="test",
        agent="test",
        harness="pi",
        prompt="synthetic",
        spawn_id="p1",
        harness_session_id="new-native",
    )

    untracked = resolve_source_use(tmp_path, "resume", "p1", "pi")

    assert isinstance(untracked, UntrackedSourceUse)
    assert untracked.original_ref == "p1"
    assert untracked.native_id == "new-native"

    _pinned_journal(tmp_path)
    spawn_store.start_spawn(
        tmp_path,
        chat_id="c8",
        model="test",
        agent="test",
        harness="pi",
        prompt="synthetic",
        spawn_id="p2",
        harness_session_id="conversation",
    )
    tracked = resolve_source_use(tmp_path, "resume", "p2", "pi")
    assert tracked == SourceUseRefused("resume", "p2", "tracked_run_unresolved")
