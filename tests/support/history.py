"""Shared history-index test helpers."""

from pathlib import Path

from meridian.lib.state.history_index import HistoryIndex


def warm_history_index(runtime_root: Path) -> None:
    """Build the history baseline outside time-bounded evidence reads."""
    HistoryIndex(runtime_root).rebuild()
