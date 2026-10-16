from uuid import NAMESPACE_URL, UUID, uuid5

from meridian.lib.idle.children import SpawnRow, active_child_count


def test_active_child_count_includes_transitive_children_and_tolerates_cycles() -> None:
    rows = (
        SpawnRow("p2", "p1", "succeeded"),
        SpawnRow("p3", "p2", "running"),
        SpawnRow("p4", "p1", "failed"),
        SpawnRow("p1", "p3", "running"),
    )

    assert active_child_count("p1", rows) == 1
    assert active_child_count(None, rows) == 0


def test_active_child_count_excludes_a_reopen_outside_the_parent_subtree() -> None:
    rows = (
        SpawnRow("p2", "p1", "running"),
        SpawnRow("p2.2", "p9", "running"),
    )

    assert active_child_count("p1", rows) == 1
    assert active_child_count("p9", rows) == 1


def test_active_child_count_follows_exact_parent_run_for_reopened_grandchild() -> None:
    def history(run_id: str) -> UUID:
        return uuid5(NAMESPACE_URL, run_id)

    rows = (
        SpawnRow("p1", None, "succeeded", history("p1")),
        SpawnRow("p2", "p1", "succeeded", history("p2"), history("p1")),
        SpawnRow("p9", None, "running", history("p9")),
        SpawnRow("p2.2", "p9", "running", history("p2.2"), history("p9")),
        SpawnRow("p10", "p2", "running", history("p10"), history("p2.2")),
    )

    assert active_child_count("p1", rows) == 0
    assert active_child_count("p9", rows) == 2
