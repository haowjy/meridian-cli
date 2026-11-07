"""Platform process-scope containment layer.

Import from the submodules: this ``__init__`` imports nothing, because a package that
imports its own children deadlocks against threads importing a child directly.
Platform-specific adapters live in:
- dispatch.py     Sync termination dispatch by containment
- posix.py        POSIX process-group termination
- windows_job.py  Windows Job Object termination
- fallback.py     Cross-platform psutil tree termination (degraded fallback)
"""
