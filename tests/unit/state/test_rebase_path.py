"""``rebase_path``: the one rule every relocate record, fold and harness target shares."""

import pytest

from meridian.lib.state.paths import rebase_path


@pytest.mark.parametrize(
    ("path", "olds", "new", "expected"),
    [
        ("/o/x", ["/o"], "/n", "/n/x"),  # plain move
        ("/o", ["/o"], "/n", "/n"),  # the directory itself
        ("/z/x", ["/o"], "/n", None),  # disjoint
        ("/o-old/x", ["/o"], "/n", None),  # whole components only
        ("/n/x", ["/o"], "/n", None),  # already moved
        ("/o/x", ["/o"], "/o", None),  # OLD equals NEW: nothing to change
        ("/r/a", ["/r"], "/r/.wt/b", "/r/.wt/b/a"),  # NEW inside OLD, first run
        ("/r/.wt/b/a", ["/r"], "/r/.wt/b", None),  # NEW inside OLD, rerun
        ("/r/sub/x", ["/r/sub"], "/r", "/r/x"),  # OLD inside NEW, first run
        ("/r/x", ["/r/sub"], "/r", None),  # OLD inside NEW, rerun
        ("/real/x", ["/typed", "/real"], "/n", "/n/x"),  # either spelling of OLD
        ("/l/.wt/f/x", ["/l", "/real"], "/real/.wt/f", None),  # rerun, other spelling
        ("/l/x", ["/l", "/real"], "/real/.wt/f", "/real/.wt/f/x"),  # first run
    ],
)
def test_rebase_path(path: str, olds: list[str], new: str, expected: str | None) -> None:
    assert rebase_path(path, olds, new) == expected
