"""RuntimeContext env override behavior for dir-without-id ambient work."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.core.context import RuntimeContext


def test_runtime_context_to_env_overrides_dir_without_work_id(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    state_dir = project_root / ".meridian"
    state_dir.mkdir(parents=True)
    (state_dir / "id").write_text("proj-runtime-ctx", encoding="utf-8")
    ambient_dir = tmp_path / "ambient-only"

    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", project_root.as_posix())
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_DIR", ambient_dir.as_posix())
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_ID", raising=False)

    ctx = RuntimeContext.from_environment()
    overrides = ctx.to_env_overrides()

    assert ctx.work_id is None
    assert ctx.work_dir == ambient_dir.resolve()
    assert overrides["MERIDIAN_ACTIVE_WORK_DIR"] == ambient_dir.as_posix()
    assert "MERIDIAN_ACTIVE_WORK_ID" not in overrides
