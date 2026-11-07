from __future__ import annotations

from meridian.lib.core.spawn_lifecycle import has_durable_report_completion
from meridian.lib.state.paths import LaunchPaths


def read_spawn_report_text(launch: LaunchPaths) -> str | None:

    try:
        return launch.report.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def spawn_report_has_durable_completion(launch: LaunchPaths) -> bool:

    return has_durable_report_completion(read_spawn_report_text(launch))
