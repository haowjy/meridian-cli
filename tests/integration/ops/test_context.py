"""Context output package-root diagnostics."""

from pathlib import Path

import pytest

from meridian.lib.ops.context import ContextInput, context_sync


@pytest.mark.parametrize(
    ("project_scope", "supported", "expected"),
    [
        (True, True, "active"),
        (True, False, "unsupported"),
        (False, True, "n/a"),
    ],
)
def test_context_reports_fallback_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    project_scope: bool,
    supported: bool,
    expected: str,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    if project_scope:
        (project_root / "mars.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(project_root)
    monkeypatch.setenv("MERIDIAN_HOME", (tmp_path / "home").as_posix())
    monkeypatch.setattr(
        "meridian.lib.catalog.mars_roots.mars_supports_fallback_root",
        lambda _executable: supported,
    )

    result = context_sync(ContextInput())

    assert result.mars_fallback == expected
