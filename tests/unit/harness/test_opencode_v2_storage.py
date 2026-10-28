"""OpenCode V2 native storage path resolution: no database opens."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.harness.opencode_transcript import resolve_opencode_db_path


def test_resolve_opencode_db_path_precedence() -> None:
    assert resolve_opencode_db_path({"OPENCODE_DB": "/tmp/custom.db"}) == Path("/tmp/custom.db")
    assert resolve_opencode_db_path({"OPENCODE_DB": ":memory:"}) == Path(":memory:")
    assert resolve_opencode_db_path(
        {"OPENCODE_DB": "nested/iso.db", "XDG_DATA_HOME": "/data"}
    ) == Path("/data/opencode/nested/iso.db")
    assert resolve_opencode_db_path(
        {"OPENCODE_DB": "nested/iso.db", "OPENCODE_HOME": "/custom/opencode"}
    ) == Path("/custom/opencode/nested/iso.db")
    assert resolve_opencode_db_path({"XDG_DATA_HOME": "/data"}) == Path(
        "/data/opencode/opencode.db"
    )
    assert resolve_opencode_db_path({"HOME": "/home/u"}) == Path(
        "/home/u/.local/share/opencode/opencode.db"
    )
