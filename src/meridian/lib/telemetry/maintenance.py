from __future__ import annotations

import threading
import time
from pathlib import Path

from meridian.lib.platform.locking import try_lock_file
from meridian.lib.telemetry.retention import run_retention_cleanup

_DEFAULT_COOLDOWN_SECONDS = 3600
_MARKER_FILENAME = ".retention-marker"


def schedule_maintenance(
    runtime_root: Path,
    *,
    cooldown_seconds: int = _DEFAULT_COOLDOWN_SECONDS,
) -> None:
    """Schedule retention cleanup if cooldown has expired.

    Returns immediately. If cleanup is needed, it runs in a daemon background
    thread. The marker file provides best-effort cross-process cooldown
    coordination; retention itself remains best-effort and silent on failure.
    """
    telemetry_dir = runtime_root / "telemetry"
    if not telemetry_dir.is_dir():
        return

    marker = telemetry_dir / _MARKER_FILENAME
    if _cooldown_active(marker, cooldown_seconds):
        return

    def _run() -> None:
        try:
            if _cooldown_active(marker, cooldown_seconds):
                return
            lock_path = telemetry_dir / ".retention-lock"
            with try_lock_file(lock_path) as lock_handle:
                if lock_handle is None:
                    return
                if _cooldown_active(marker, cooldown_seconds):
                    return
                run_retention_cleanup(telemetry_dir, runtime_root=runtime_root)
                _update_marker(marker)
        except Exception:
            return

    thread = threading.Thread(
        target=_run,
        daemon=True,
        name="telemetry-maintenance",
    )
    thread.start()


def _cooldown_active(marker: Path, cooldown_seconds: int) -> bool:
    try:
        mtime = marker.stat().st_mtime
    except (FileNotFoundError, OSError):
        return False
    return (time.time() - mtime) < cooldown_seconds


def _update_marker(marker: Path) -> None:
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass
