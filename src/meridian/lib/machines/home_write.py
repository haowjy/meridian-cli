"""Writes under the home that never fail the call that made them."""

from __future__ import annotations

import logging
from collections.abc import Callable

from meridian.lib.state.saved_machines import SavedMachinesError

logger = logging.getLogger(__name__)


def best_effort(what: str, write: Callable[[], object]) -> None:
    """Run *write*; a failure (a read-only home, a damaged list) is one warning naming *what*.
    The cache and the rename correction are never needed for the call in hand to be right, and
    the next call tries again."""

    try:
        write()
    except (OSError, SavedMachinesError) as exc:
        logger.warning("can't %s: %s", what, exc)


__all__ = ["best_effort"]
