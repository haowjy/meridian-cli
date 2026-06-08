"""Cycle-safe spawn descendant traversal helpers."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence

from meridian.lib.state.spawn.model import SpawnRecord


def iter_descendants_from_parent_map(
    root_id: str,
    by_parent: Mapping[str | None, Sequence[SpawnRecord]],
) -> Iterator[SpawnRecord]:
    """Yield transitive child rows below ``root_id`` once per spawn id.

    Spawn parent links are persisted data and may be malformed. Treat cycles as
    corrupt rows to walk past, not as a reason for readers to wedge forever.
    The root id starts as visited so a cycle back to the root does not report
    the root as its own descendant.
    """

    visited = {root_id}
    stack = [root_id]
    while stack:
        parent_id = stack.pop()
        for child in by_parent.get(parent_id, ()):
            child_id = child.id
            if child_id in visited:
                continue
            visited.add(child_id)
            yield child
            stack.append(child_id)


__all__ = ["iter_descendants_from_parent_map"]
