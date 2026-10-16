"""Legacy Windows Job Object linkage; POSIX launchers use launch_gate."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ParentDeathLink:
    """Keep the legacy Windows Job Object handle alive for the child lifetime."""

    job_name: str | None = None
    job_handle: object | None = None


def link_child_lifetime_to_parent(pid: int) -> ParentDeathLink:
    """Assign a Windows child to a kill-on-close Job Object."""
    from meridian.lib.platform.process_scope.windows_job import assign_to_new_job

    result = assign_to_new_job(pid)
    if result is None:
        return ParentDeathLink()
    job_name, job_handle = result
    return ParentDeathLink(job_name=job_name, job_handle=job_handle)
