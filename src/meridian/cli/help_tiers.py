"""Shared CLI help-tier controls."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from cyclopts import Group

# Hidden by default; curated help reveals this group at render time for human and
# agent advanced spawn help.
ADVANCED_PARAMS = Group("Advanced", show=False)


def advanced_params_visible(*, agent_mode: bool, advanced: bool) -> bool:
    """Whether the Advanced parameter panel should appear in curated help."""

    return (not agent_mode) or advanced


@contextmanager
def advanced_params_visibility(
    *,
    agent_mode: bool,
    advanced: bool,
) -> Generator[None, None, None]:
    """Temporarily reveal the Advanced parameter group for help assembly."""

    reveal = advanced_params_visible(agent_mode=agent_mode, advanced=advanced)
    prior_show = ADVANCED_PARAMS._show
    object.__setattr__(ADVANCED_PARAMS, "_show", reveal)
    try:
        yield
    finally:
        object.__setattr__(ADVANCED_PARAMS, "_show", prior_show)


