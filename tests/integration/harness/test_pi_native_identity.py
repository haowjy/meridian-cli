"""Exact Pi identity at the native filesystem boundary."""

from pathlib import Path

import pytest

from meridian.lib.harness.pi_identity import mint_session_id, resolve_session_file


@pytest.mark.parametrize("basename", ["unrelated.jsonl", ".jsonl"])
def test_header_collision_ignores_basename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, basename: str,
) -> None:
    monkeypatch.setattr("meridian.lib.harness.pi_identity.uuid.uuid4", lambda: "chosen-id")
    (tmp_path / basename).write_text('{"type":"session","id":"chosen-id"}\n')
    with pytest.raises(ValueError, match="native_identity_collision"):
        mint_session_id(tmp_path)


def test_exact_file_refuses_missing_ambiguous_and_replaced_header(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="native_transcript_missing"):
        resolve_session_file(tmp_path, "chosen-id")
    first = tmp_path / "1_chosen-id.jsonl"
    first.write_text('{"type":"session","id":"replacement"}\n')
    with pytest.raises(ValueError, match="entry_mismatch"):
        resolve_session_file(tmp_path, "chosen-id")
    first.write_text('{"type":"session","id":"chosen-id"}\n')
    assert resolve_session_file(tmp_path, "chosen-id") == first
    (tmp_path / "2_chosen-id.jsonl").write_text(first.read_text())
    with pytest.raises(ValueError, match="ambiguous_native_file"):
        resolve_session_file(tmp_path, "chosen-id")


@pytest.mark.parametrize("bound_store", [True, False])
@pytest.mark.parametrize("chat_reference", [True, False])
def test_reference_store_comes_from_exact_binding_not_primary_metadata(
    tmp_path: Path,
    bound_store: bool,
    chat_reference: bool,
) -> None:
    from meridian.lib.ops.reference import resolve_session_reference
    from meridian.lib.state import session_store, spawn_store
    from meridian.lib.state.primary_meta import PrimaryMetadata, write_primary_metadata

    store = str(tmp_path / "pinned-store") if bound_store else None
    key = spawn_store.start_spawn(
        tmp_path,
        chat_id="c1",
        harness="pi",
        harness_session_id="native-id",
        model="test",
        agent="",
        kind="primary",
        prompt="test",
    )
    session_store.start_session(
        tmp_path,
        "pi",
        "native-id",
        "test",
        chat_id="c1",
        spawn_id=key,
        native_store=store,
    )
    write_primary_metadata(
        tmp_path / "spawns" / key, PrimaryMetadata(session_dir=str(tmp_path / "wrong"))
    )
    reference = resolve_session_reference(
        tmp_path,
        "c1" if chat_reference else key,
        runtime_root=tmp_path,
    )
    assert reference.source_native_store == store


@pytest.mark.parametrize("header", ["", "torn", "[]", '{"type":"session"}'])
def test_mint_warns_and_skips_unreadable_sibling_but_exact_read_refuses(
    tmp_path: Path, header: str,
) -> None:
    from structlog.testing import capture_logs

    unreadable = tmp_path / "1_unreadable-id.jsonl"
    unreadable.write_text(header)
    with capture_logs() as logs:
        assert mint_session_id(tmp_path)
    assert any(
        event.get("event") == "pi_store_unreadable_header"
        and event.get("path") == str(unreadable)
        and event.get("log_level") == "warning"
        for event in logs
    )
    with pytest.raises(ValueError, match="entry_mismatch"):
        resolve_session_file(tmp_path, "unreadable-id")
