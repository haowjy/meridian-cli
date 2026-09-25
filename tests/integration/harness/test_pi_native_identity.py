"""Exact Pi identity at the native filesystem boundary."""
from pathlib import Path

import pytest

from meridian.lib.harness.pi_identity import mint_session_id, resolve_session_file


def test_header_collision_ignores_basename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('meridian.lib.harness.pi_identity.uuid.uuid4', lambda: 'chosen-id')
    (tmp_path / 'unrelated.jsonl').write_text('{"type":"session","id":"chosen-id"}\n')
    with pytest.raises(ValueError, match='native_identity_collision'):
        mint_session_id(tmp_path)


def test_exact_file_refuses_missing_ambiguous_and_replaced_header(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match='native_transcript_missing'):
        resolve_session_file(tmp_path, 'chosen-id')
    first = tmp_path / '1_chosen-id.jsonl'
    first.write_text('{"type":"session","id":"replacement"}\n')
    with pytest.raises(ValueError, match='entry_mismatch'):
        resolve_session_file(tmp_path, 'chosen-id')
    first.write_text('{"type":"session","id":"chosen-id"}\n')
    assert resolve_session_file(tmp_path, 'chosen-id') == first
    (tmp_path / '2_chosen-id.jsonl').write_text(first.read_text())
    with pytest.raises(ValueError, match='ambiguous_native_file'):
        resolve_session_file(tmp_path, 'chosen-id')
