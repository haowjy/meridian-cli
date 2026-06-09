"""Managed-primary fallback diagnostic preservation tests."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.launch.constants import OUTPUT_FILENAME, PRIMARY_META_FILENAME
from meridian.lib.launch.process import runner as runner_module


def test_cleanup_managed_primary_sidecars_preserves_stderr_diagnostic(tmp_path: Path) -> None:
    spawn_dir = tmp_path / "spawn"
    spawn_dir.mkdir()
    primary_meta = spawn_dir / PRIMARY_META_FILENAME
    output = spawn_dir / OUTPUT_FILENAME
    stderr = spawn_dir / "stderr.log"
    primary_meta.write_text('{"managed_backend":true}\n', encoding="utf-8")
    output.write_text('{"event":"partial"}\n', encoding="utf-8")
    stderr.write_text("backend failed before attach\n", encoding="utf-8")

    runner_module._cleanup_managed_primary_sidecars(spawn_dir)

    assert not primary_meta.exists()
    assert not output.exists()
    assert stderr.read_text(encoding="utf-8") == "backend failed before attach\n"


def test_managed_primary_stderr_excerpt_is_bounded(tmp_path: Path) -> None:
    spawn_dir = tmp_path / "spawn"
    spawn_dir.mkdir()
    (spawn_dir / "stderr.log").write_text("x" * 20, encoding="utf-8")

    assert runner_module._managed_primary_stderr_excerpt(spawn_dir, max_chars=5) == "xxxxx…"


def test_managed_primary_stderr_excerpt_preserves_tail_diagnostics(tmp_path: Path) -> None:
    spawn_dir = tmp_path / "spawn"
    spawn_dir.mkdir()
    stderr = spawn_dir / "stderr.log"
    stderr.write_bytes(b"a" * 20_000 + b"tail diagnostics")

    excerpt = runner_module._managed_primary_stderr_excerpt(spawn_dir, max_chars=40)

    assert excerpt is not None
    assert len(excerpt) <= 41
    assert excerpt.startswith("…")
    assert "tail diagnostics" in excerpt
