"""Exact Pi source qualification against isolated synthetic stores."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

from meridian.lib.harness import pi_native_source as source
from meridian.lib.state.session_authority import QualifiedLocalFile


def _journal(path: Path, *, session_id: str = "A", version: int = 3) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(
        {"type": "session", "version": version, "id": session_id, "cwd": "/tmp"}
    ).encode() + b"\n{truncated tail"
    path.write_bytes(data)
    return data


def _qualified(store: Path, path: Path, session_id: str = "A") -> QualifiedLocalFile:
    result = source.qualify_pi_source(
        effective_store=store, session_id=session_id, session_file=str(path)
    )
    assert isinstance(result, source.PiSourceQualified)
    return result.observation


def test_qualifies_exact_nested_file_header_and_preserves_bytes(tmp_path: Path) -> None:
    store = tmp_path / "sessions"
    file = store / "nested" / "chat.jsonl"
    before = _journal(file, version=1)
    qualified = _qualified(store, file)

    assert qualified.path == str(file)
    assert source.preflight_pi_source(
        qualified, effective_store=store, session_id="A"
    ).status == "eligible"
    assert file.read_bytes() == before


def test_symlink_is_resolved_once_but_pinned_traversal_rejects_retarget(
    tmp_path: Path,
) -> None:
    store = tmp_path / "sessions"
    actual = store / "nested" / "actual.jsonl"
    _journal(actual)
    alias = store / "alias.jsonl"
    alias.symlink_to(actual)

    pinned = _qualified(store, alias)
    assert pinned.path == str(actual)
    other = store / "nested" / "other.jsonl"
    _journal(other)
    alias.unlink()
    alias.symlink_to(other)
    assert source.preflight_pi_source(
        pinned, effective_store=store, session_id="A"
    ).status == "eligible"

    actual.unlink()
    actual.symlink_to(other)
    status = source.preflight_pi_source(pinned, effective_store=store, session_id="A")
    assert status.status == "unavailable"
    assert status.reason == "file_changed"


def test_rejects_escape_prefix_trap_wrong_and_malformed_headers(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    outside = tmp_path / "sessions-copy" / "a.jsonl"
    _journal(outside)
    assert isinstance(
        source.qualify_pi_source(
            effective_store=root, session_id="A", session_file=str(outside)
        ),
        source.PiSourceUnavailable,
    )

    wrong = root / "wrong.jsonl"
    _journal(wrong, session_id="B")
    assert source.qualify_pi_source(
        effective_store=root, session_id="A", session_file=str(wrong)
    ) == source.PiSourceUnavailable("identity_mismatch")
    malformed = root / "malformed.jsonl"
    malformed.write_text('{"type":"session","id":\n')
    assert source.qualify_pi_source(
        effective_store=root, session_id="A", session_file=str(malformed)
    ) == source.PiSourceUnavailable("invalid_native_source")


def test_missing_is_pending_and_same_id_claims_keep_exact_distinct_files(
    tmp_path: Path,
) -> None:
    store = tmp_path / "sessions"
    store.mkdir()
    missing = source.qualify_pi_source(
        effective_store=store,
        session_id="A",
        session_file=str(store / "nested" / "future.jsonl"),
    )
    assert isinstance(missing, source.PiSourcePending)
    assert missing.observation.path.endswith("/nested/future.jsonl")

    first = store / "first.jsonl"
    second = store / "second.jsonl"
    _journal(first)
    _journal(second)
    assert _qualified(store, first).file_object != _qualified(store, second).file_object


def test_root_and_file_replacement_are_refused(tmp_path: Path) -> None:
    store = tmp_path / "sessions"
    file = store / "chat.jsonl"
    _journal(file)
    pinned = _qualified(store, file)
    moved = store / "old.jsonl"
    file.rename(moved)
    _journal(file)
    assert source.preflight_pi_source(
        pinned, effective_store=store, session_id="A"
    ).reason == "file_changed"

    new_store = tmp_path / "new-store"
    store.rename(new_store)
    store.mkdir()
    _journal(store / "chat.jsonl")
    assert source.preflight_pi_source(
        pinned, effective_store=store, session_id="A"
    ).reason == "store_changed"


def test_intermediate_symlink_insertion_is_refused(tmp_path: Path) -> None:
    store = tmp_path / "sessions"
    file = store / "nested" / "chat.jsonl"
    _journal(file)
    pinned = _qualified(store, file)
    nested = store / "nested"
    moved = store / "saved-nested"
    nested.rename(moved)
    nested.symlink_to(moved, target_is_directory=True)

    status = source.preflight_pi_source(pinned, effective_store=store, session_id="A")
    assert status.status == "unavailable"
    assert status.reason == "file_changed"


def test_absent_pinned_file_is_missing_and_discovery_is_never_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = tmp_path / "sessions"
    file = store / "chat.jsonl"
    _journal(file)
    pinned = _qualified(store, file)
    file.unlink()
    def deny_discovery(*args: object, **kwargs: object) -> object:
        raise AssertionError("Pi source qualification must not discover files")

    monkeypatch.setattr(Path, "glob", deny_discovery)
    monkeypatch.setattr(Path, "rglob", deny_discovery)
    status = source.preflight_pi_source(pinned, effective_store=store, session_id="A")
    assert status.status == "unavailable"
    assert status.reason == "missing"


def test_permission_is_not_reported_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "sessions"
    file = store / "chat.jsonl"
    _journal(file)
    def denied(*args: object, **kwargs: object) -> int:
        raise PermissionError(13, "denied")

    monkeypatch.setattr(source, "_open_file", denied)
    status = source.qualify_pi_source(
        effective_store=store, session_id="A", session_file=str(file)
    )
    assert status == source.PiSourceUnavailable("inaccessible")
