"""Telemetry status reporting."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.core.util import FormatContext
from meridian.lib.state.liveness import is_process_alive
from meridian.lib.state.spawn_status import read_derived
from meridian.lib.telemetry.reader import discover_segments
from meridian.lib.telemetry.retention import parse_segment_owner

ROOTLESS_LIMITATION_NOTE = (
    "Rootless processes (MCP stdio server) emit telemetry to stderr only "
    "and are outside the scope of local segment readers."
)


@dataclass(frozen=True)
class TelemetryStatus:
    """Health summary of the local telemetry sink."""

    telemetry_dir: Path | list[Path]
    segment_count: int
    total_bytes: int
    active_writers: list[str]
    legacy_segments: int = 0
    rootless_note: str = ROOTLESS_LIMITATION_NOTE

    @property
    def total_size_human(self) -> str:
        """Human-readable total size."""
        if self.total_bytes < 1024:
            return f"{self.total_bytes} B"
        if self.total_bytes < 1024 * 1024:
            return f"{self.total_bytes / 1024:.1f} KB"
        return f"{self.total_bytes / (1024 * 1024):.1f} MB"

    def format_text(self, ctx: FormatContext | None = None) -> str:
        """Render a human-readable telemetry health summary."""
        _ = ctx
        return format_status_dict(status_to_dict(self))


def status_to_dict(status: TelemetryStatus) -> dict[str, Any]:
    """Return a JSON-serializable telemetry status payload."""
    result = asdict(status)
    telemetry_dir = result["telemetry_dir"]
    if isinstance(telemetry_dir, list):
        directories = cast("list[object]", telemetry_dir)
        result["telemetry_dir"] = [str(directory) for directory in directories]
    else:
        result["telemetry_dir"] = str(telemetry_dir)
    result["total_size_human"] = status.total_size_human
    return result


def format_status_dict(status: dict[str, Any]) -> str:
    """Render a telemetry status payload as human-readable text."""
    active_writers = status.get("active_writers", [])
    if isinstance(active_writers, list):
        writer_values = cast("list[object]", active_writers)
        active = ", ".join(str(writer) for writer in writer_values) or "none"
    else:
        active = "none"
    telemetry_dir = status.get("telemetry_dir", "")
    if isinstance(telemetry_dir, list):
        directories = cast("list[object]", telemetry_dir)
        dir_str = ", ".join(str(directory) for directory in directories)
    else:
        dir_str = str(telemetry_dir)
    lines = [
        f"Telemetry directory: {dir_str}",
        f"Segment count: {status.get('segment_count', 0)}",
        f"Total size: {status.get('total_size_human', '')}",
        f"Active writers: {active}",
        f"Note: {status.get('rootless_note', ROOTLESS_LIMITATION_NOTE)}",
    ]
    legacy = status.get("legacy_segments", 0)
    if isinstance(legacy, int) and legacy > 0:
        lines.append(
            "Legacy: "
            f"{legacy} segment(s) in ~/.meridian/telemetry/ from a previous version "
            "(will age out via retention)"
        )
    return "\n".join(lines)


def compute_status(
    runtime_root: Path,
    *,
    telemetry_dirs: list[Path] | None = None,
    legacy_dir: Path | None = None,
) -> TelemetryStatus:
    """Compute telemetry sink status from disk state."""
    dirs = telemetry_dirs if telemetry_dirs is not None else [runtime_root / "telemetry"]
    segments: list[Path] = []
    for directory in dirs:
        segments.extend(discover_segments(directory))

    total_bytes = 0
    writers: set[str] = set()
    for segment in segments:
        try:
            total_bytes += segment.stat().st_size
        except OSError:
            continue
        owner = parse_segment_owner(segment)
        if owner is None:
            continue
        writer = f"{owner.logical_owner}.{owner.pid}"
        if owner.is_cli_or_chat:
            if is_process_alive(owner.pid):
                writers.add(writer)
        else:
            # Segments live at <runtime_root>/telemetry/<segment>.jsonl. Derive
            # the runtime root from each segment so global status checks spawn
            # liveness against the owning project's runtime state.
            segment_runtime_root = segment.parent.parent
            spawn = read_derived(segment_runtime_root, owner.logical_owner)
            if spawn is not None and is_active_spawn_status(spawn.status):
                writers.add(writer)

    legacy_count = 0
    if legacy_dir is not None and legacy_dir.is_dir():
        legacy_count = len(discover_segments(legacy_dir))

    return TelemetryStatus(
        telemetry_dir=dirs[0] if len(dirs) == 1 else dirs,
        segment_count=len(segments),
        total_bytes=total_bytes,
        active_writers=sorted(writers),
        legacy_segments=legacy_count,
    )
