from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from meridian.lib.core.native_identity import NativeKey
from meridian.lib.harness.native_witness import FileWitness, OpenCodeV1Witness, OpenCodeV2Witness
from meridian.lib.state.native_search_index import (
    PARSER_VERSION,
    NativeSearchIndex,
    NativeSearchUnavailable,
    TranscriptEntry,
    normalize_index_text,
    search_text,
    sqlite_search_supported,
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
        witness=FileWitness(1, 2, 3, 4).encode(),
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
        rows = index.search(query)
        expected = query.lower() in search_text(text)
        assert [row.content for row in rows] == ([text] if expected else [])
    assert normalize_index_text(text).count("\0") == 0


def test_short_and_nul_queries_scan_and_preserve_exact_predicate(tmp_path: Path) -> None:
    index = NativeSearchIndex(tmp_path / "index.sqlite3")
    key = NativeKey("pi", "/native", "two")
    values = ["x alpha\nβeta", "nul\0inside", "empty"]
    index.replace_source(
        key,
        locator="/native/file",
        witness=FileWitness(1, 2, 3, 4).encode(),
        activity=0,
        entries=[TranscriptEntry(i, value) for i, value in enumerate(values)],
    )
    for query in ("x", "βe", "\0", "nul\0inside"):
        rows = index.search(query)
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
        witness=witness.encode(),
        activity=1,
        entries=[TranscriptEntry(0, "obsolete value")],
    )
    index.replace_source(
        key,
        locator="b",
        witness=FileWitness(1, 2, 4, 4).encode(),
        activity=2,
        entries=[TranscriptEntry(1, "current value")],
    )
    assert index.search("obsolete") == []
    assert index.search("current")[0].ordinal == 1
    assert index.inventory()[key].is_current(FileWitness(1, 2, 4, 4).encode())
    assert not index.inventory()[key].is_current(witness.encode())
    assert index.counts() == (1, 1)
    index.rebuild()
    assert index.counts() == (0, 0)
    assert index.search("current") == []


def test_witness_families_are_distinct_and_parser_version_invalidates(tmp_path: Path) -> None:
    index = NativeSearchIndex(tmp_path / "index.sqlite3")
    key = NativeKey("opencode", "/db", "session")
    v1 = OpenCodeV1Witness(4, 90, 2, 80, 100)
    v2 = OpenCodeV2Witness(2, 8, 80, 100)
    assert v1.encode() != v2.encode()
    index.replace_source(
        key,
        locator="/db",
        witness=v1.encode(),
        activity=100,
        entries=[TranscriptEntry(0, "opencode content")],
    )
    assert index.inventory()[key].is_current(v1.encode())
    assert not index.inventory()[key].is_current(v2.encode())
    assert not replace(index.inventory()[key], parser_version=PARSER_VERSION + 1).is_current(
        v1.encode()
    )


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
        witness=witness.encode(),
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
            witness=FileWitness(1, 2, 4, 4).encode(),
            activity=2,
            entries=[TranscriptEntry(1, "replacement")],
        )
    with sqlite3.connect(index.path) as db:
        db.execute("DROP TRIGGER fail_entry")
    assert index.search("survives failure")
    assert index.search("replacement") == []
    assert index.counts() == (1, 1)


def test_scoped_search_accepts_5000_keys_and_excludes_outside_keys(tmp_path: Path) -> None:
    index = NativeSearchIndex(tmp_path / "index.sqlite3")
    keys = [NativeKey("codex", "/native", str(i)) for i in range(5000)]
    for key in (keys[-1], NativeKey("codex", "/elsewhere", "4999")):
        index.replace_source(
            key,
            locator="file",
            witness=FileWitness(1, 2, 3, 4).encode(),
            activity=0,
            entries=[TranscriptEntry(1, "needle")],
        )
    assert [r.key for r in index.search("needle", keys=keys)] == [keys[-1]]


def test_duplicate_refresh_does_not_consume_entries(tmp_path: Path) -> None:
    index = NativeSearchIndex(tmp_path / "index.sqlite3")
    key = NativeKey("codex", "/native", "one")
    witness = FileWitness(1, 2, 3, 4)
    index.replace_source(
        key,
        locator="file",
        witness=witness.encode(),
        activity=0,
        entries=[TranscriptEntry(1, "original")],
    )
    index.replace_source(
        key,
        locator="file",
        witness=witness.encode(),
        activity=0,
        entries=[TranscriptEntry(1, "wrong duplicate")],
    )
    assert index.search("original")
    assert not index.search("wrong")


def test_rebuild_recreates_projection_without_free_pages(tmp_path):
    index = NativeSearchIndex.for_runtime(tmp_path)
    index.replace_source(
        NativeKey("claude", "/native", "one"),
        locator="file",
        witness="opaque",
        activity=0,
        entries=[TranscriptEntry(1, "large transcript " * 100_000)],
    )
    before = index.path.stat().st_size
    index.rebuild()
    assert index.path.stat().st_size < before
    assert index.counts() == (0, 0)
