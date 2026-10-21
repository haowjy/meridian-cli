"""Launch failures surfaced without importing composition or execution."""


class WorkTaskDirMissing(Exception):
    """No usable fallback exists for a stale work-item task directory."""
