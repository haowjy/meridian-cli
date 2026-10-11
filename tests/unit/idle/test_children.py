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
