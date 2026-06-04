"""File-backed spawn report reads."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.core.spawn_lifecycle import has_durable_report_completion


def read_spawn_report_text(runtime_root: Path, spawn_id: str) -> str | None:
    """Read a spawn's persisted report text if available."""

    report_path = runtime_root / "spawns" / spawn_id / "report.md"
    try:
        return report_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def spawn_report_has_durable_completion(runtime_root: Path, spawn_id: str) -> bool:
    """Return True when a spawn has a durable completion report on disk."""

    return has_durable_report_completion(read_spawn_report_text(runtime_root, spawn_id))
