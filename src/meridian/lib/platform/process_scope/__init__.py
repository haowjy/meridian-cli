"""Platform process-scope containment layer.

Import from the submodules: this ``__init__`` imports nothing, because a package that
imports its own children deadlocks against threads importing a child directly.
Platform-specific adapters live in:
- dispatch.py     Sync termination dispatch by containment
- posix.py        POSIX process-group termination
- windows_job.py  Windows Job Object termination
- fallback.py     The one psutil tree termination, sync and async; every path uses it
- base.py         Snapshot types and check_root_identity, the birth-time gate for every signal
"""
