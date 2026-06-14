"""Shared CLI help-tier controls."""

from cyclopts import Group

# Hidden by default; revealed at render time for human help and ``spawn -h --advanced``.
ADVANCED_PARAMS = Group("Advanced", show=False)
