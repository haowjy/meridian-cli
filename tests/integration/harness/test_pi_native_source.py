"""Exact Pi source qualification against isolated synthetic stores."""

from __future__ import annotations

import json
from pathlib import Path

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
    root.mkdir()
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


@pytest.mark.parametrize("missing_depth", [1, 2])
def test_missing_intermediate_path_is_pending_despite_unrelated_same_basename(
    tmp_path: Path, missing_depth: int
) -> None:
    store = tmp_path / "sessions"
    store.mkdir()
    _journal(store / "future.jsonl")
    missing_parts = (f"missing-{index}" for index in range(missing_depth))
    selected = store.joinpath(*missing_parts, "future.jsonl")

    result = source.qualify_pi_source(
        effective_store=store, session_id="A", session_file=str(selected)
    )

    assert isinstance(result, source.PiSourcePending)
    assert result.observation.path == str(selected)


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

    monkeypatch.setattr(source, "_open_file_with_directories", denied)
    status = source.qualify_pi_source(
        effective_store=store, session_id="A", session_file=str(file)
    )
    assert status == source.PiSourceUnavailable("inaccessible")


@pytest.mark.parametrize("operation", ["acquire", "preflight"])
@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("file", "file_changed"),
        ("root", "store_changed"),
        ("parent", "file_changed"),
        ("root_missing", "missing"),
    ],
)
def test_namespace_replacement_during_header_read_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    mutation: str,
    expected: str,
) -> None:
    store = tmp_path / "sessions"
    file = store / "nested" / "chat.jsonl"
    _journal(file)
    pinned = _qualified(store, file)
    real_read_header = source._read_header

    def replace_after_read(fd: int) -> dict[str, object] | None:
        header = real_read_header(fd)
        if mutation == "file":
            file.rename(file.with_name("saved.jsonl"))
            _journal(file, session_id="B")
        elif mutation.startswith("root"):
            store.rename(tmp_path / "saved-store")
            if mutation == "root":
                _journal(file, session_id="B")
        else:
            (store / "nested").rename(store / "saved-nested")
            _journal(file, session_id="B")
        return header

    monkeypatch.setattr(source, "_read_header", replace_after_read)
    if operation == "acquire":
        result = source.qualify_pi_source(
            effective_store=store, session_id="A", session_file=str(file)
        )
        assert isinstance(result, source.PiSourceUnavailable)
        assert result.reason == expected
    else:
        result = source.preflight_pi_source(
            pinned, effective_store=store, session_id="A"
        )
        assert result.status == "unavailable"
        assert result.reason == expected


def test_missing_pending_path_is_reobserved_after_parent_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "sessions"
    store.mkdir()
    selected = store / "nested" / "future.jsonl"
    real_reobserve = source._reobserve_namespace

    def replace_before_reobserve(
        root: Path,
        path: Path,
        root_fd: int,
        directories: list[int],
        leaf_fd: int | None,
        missing_from: int | None,
    ) -> str | None:
        (store / "nested").mkdir()
        return real_reobserve(root, path, root_fd, directories, leaf_fd, missing_from)

    monkeypatch.setattr(source, "_reobserve_namespace", replace_before_reobserve)
    result = source.qualify_pi_source(
        effective_store=store, session_id="A", session_file=str(selected)
    )
    assert isinstance(result, source.PiSourceUnavailable)
    assert result.reason == "file_changed"


def test_pending_root_replacement_is_reobserved_at_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "sessions"
    store.mkdir()
    selected = store / "future.jsonl"
    real_reobserve = source._reobserve_namespace

    def replace_root_before_reobserve(
        root: Path,
        path: Path,
        root_fd: int,
        directories: list[int],
        leaf_fd: int | None,
        missing_from: int | None,
    ) -> str | None:
        store.rename(tmp_path / "saved-store")
        store.mkdir()
        return real_reobserve(root, path, root_fd, directories, leaf_fd, missing_from)

    monkeypatch.setattr(source, "_reobserve_namespace", replace_root_before_reobserve)
    result = source.qualify_pi_source(
        effective_store=store, session_id="A", session_file=str(selected)
    )
    assert isinstance(result, source.PiSourceUnavailable)
    assert result.reason == "store_changed"


def test_fifo_sources_return_without_blocking(tmp_path: Path) -> None:
    import subprocess
    import sys

    store = tmp_path / "sessions"
    store.mkdir()
    fifo = store / "chat.jsonl"
    import os

    os.mkfifo(fifo)
    script = """
from pathlib import Path
from meridian.lib.harness.pi_native_source import (
    PiSourceUnavailable,
    preflight_pi_source,
    qualify_pi_source,
)
from meridian.lib.state.session_authority import QualifiedLocalFile, LocalObjectStamp
import sys
store, fifo = Path(sys.argv[1]), Path(sys.argv[2])
acquired = qualify_pi_source(effective_store=store, session_id='A', session_file=str(fifo))
assert acquired == PiSourceUnavailable('invalid_native_source'), acquired
pinned = QualifiedLocalFile(
    kind='local_file',
    path=str(fifo),
    store_object=LocalObjectStamp(
        device=store.stat().st_dev, inode=store.stat().st_ino
    ),
    file_object=LocalObjectStamp(device=0, inode=0),
    rule='pi_rpc_exact_v1',
)
checked = preflight_pi_source(pinned, effective_store=store, session_id='A')
assert checked.status == 'unavailable' and checked.reason == 'invalid_native_source', checked
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(store), str(fifo)],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
