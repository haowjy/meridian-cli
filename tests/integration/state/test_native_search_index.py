from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from meridian.lib.core.native_identity import NativeKey
from meridian.lib.state.native_search_index import (
    FileWitness,
    NativeSearchIndex,
    NativeSearchUnavailable,
    OpenCodeV1Witness,
    OpenCodeV2Witness,
    TranscriptEntry,
    normalize_index_text,
    search_text,
    sqlite_search_supported,
    witness_json,
)


def test_fts_verification_matches_predicate_for_unicode_nul_whitespace_and_syntax(
    tmp_path: Path,
) -> None:
    index = NativeSearchIndex(tmp_path / "index.sqlite3")
    key = NativeKey("codex", "/native", "one")
    text = (
        'before\nİ Cherokee \u13a0\u13a1 Georgian \u1c90\u1c91 after\0tail  quote " * - NEAR( x:y'
    )
    index.replace_source(
        key,
        locator="/native/file",
        witness=FileWitness(1, 2, 3, 4),
        activity=7,
        entries=[TranscriptEntry(0, text)],
    )

    for query in (
        "İ Cherokee",
        "\u13a0\u13a1 Georgian",
        "\u1c90\u1c91 after",
        "tail quote",
        'quote " *',
        "* - NEAR",
        "NEAR( x:y",
        "before after",
        "no hit",
    ):
        mode, rows = index.search(query)
        expected = query.lower() in search_text(text)
        assert mode == "fts"
        assert [row.content for row in rows] == ([text] if expected else [])
    assert normalize_index_text(text).count("\0") == 0


def test_short_and_nul_queries_scan_and_preserve_exact_predicate(tmp_path: Path) -> None:
    index = NativeSearchIndex(tmp_path / "index.sqlite3")
    key = NativeKey("pi", "/native", "two")
    values = ["x alpha\nβeta", "nul\0inside", "empty"]
    index.replace_source(
        key,
        locator="/native/file",
        witness=FileWitness(1, 2, 3, 4),
        activity=0,
        entries=[TranscriptEntry(i, value) for i, value in enumerate(values)],
    )
    for query in ("x", "βe", "\0", "nul\0inside"):
        mode, rows = index.search(query)
        assert mode == "scan"
        assert [row.content for row in rows] == [
            value for value in values if query.lower() in search_text(value)
        ]


def test_source_replacement_and_rebuild_remove_old_fts_rows(tmp_path: Path) -> None:
    index = NativeSearchIndex(tmp_path / "index.sqlite3")
    key = NativeKey("claude", "/native", "three")
    witness = FileWitness(1, 2, 3, 4)
    index.replace_source(
        key,
        locator="a",
        witness=witness,
        activity=1,
        entries=[TranscriptEntry(0, "obsolete value")],
    )
    index.replace_source(
        key, locator="b", witness=witness, activity=2, entries=[TranscriptEntry(1, "current value")]
    )
    assert index.search("obsolete")[1] == []
    assert index.search("current")[1][0].ordinal == 1
    assert index.is_fresh(key, witness)
    assert not index.is_fresh(key, FileWitness(1, 2, 4, 4))
    assert index.counts() == (1, 1)
    index.rebuild()
    assert index.counts() == (0, 0)
    assert index.search("current")[1] == []


def test_witness_families_are_distinct_and_parser_version_invalidates(tmp_path: Path) -> None:
    index = NativeSearchIndex(tmp_path / "index.sqlite3")
    key = NativeKey("opencode", "/db", "session")
    v1 = OpenCodeV1Witness(4, 90, 2, 80, 100)
    v2 = OpenCodeV2Witness(2, 8, 80, 100)
    assert witness_json(v1) != witness_json(v2)
    index.replace_source(
        key,
        locator="/db",
        witness=v1,
        activity=100,
        entries=[TranscriptEntry(0, "opencode content")],
    )
    assert index.is_fresh(key, v1)
    assert not index.is_fresh(key, v2)
    assert not index.is_fresh(key, v1, parser_version=2)


def test_old_sqlite_reports_index_unavailable_without_creating_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import meridian.lib.state.native_search_index as native_search_index

    monkeypatch.setattr(native_search_index.sqlite3, "sqlite_version_info", (3, 42, 0))
    assert not sqlite_search_supported()
    path = tmp_path / "old.sqlite3"
    with pytest.raises(NativeSearchUnavailable):
        NativeSearchIndex(path)
    assert not path.exists()


def test_failed_replace_keeps_previous_source_and_fts_consistent(tmp_path: Path) -> None:
    index = NativeSearchIndex(tmp_path / "index.sqlite3")
    key = NativeKey("codex", "/native", "four")
    witness = FileWitness(1, 2, 3, 4)
    index.replace_source(
        key,
        locator="a",
        witness=witness,
        activity=1,
        entries=[TranscriptEntry(0, "survives failure")],
    )
    # Fail after the transactional deletes to exercise rollback of both indexes.
    with sqlite3.connect(index.path) as db:
        db.execute(
            "CREATE TRIGGER fail_entry BEFORE INSERT ON entries "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        index.replace_source(
            key,
            locator="b",
            witness=witness,
            activity=2,
            entries=[TranscriptEntry(1, "replacement")],
        )
    with sqlite3.connect(index.path) as db:
        db.execute("DROP TRIGGER fail_entry")
    assert index.search("survives failure")[1]
    assert index.search("replacement")[1] == []
    assert index.counts() == (1, 1)
