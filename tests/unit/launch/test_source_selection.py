from pathlib import Path

import pytest

from meridian.lib.launch.source_selection import (
    PrimarySourceSelection,
    reconcile_primary_source_selection,
)
from meridian.lib.ops.reference import UntrackedSourceUse


def test_untracked_source_requires_every_local_identity_fact_to_agree(tmp_path: Path) -> None:
    authorization = UntrackedSourceUse(
        operation="resume",
        original_ref="native-A",
        native_id="native-A",
        harness="h1",
        lookup_scope=tmp_path,
    )
    base = PrimarySourceSelection(
        source_ref="native-A",
        native_id="native-A",
        operation="resume",
        harness="h1",
        runtime_root=tmp_path,
    )

    assert reconcile_primary_source_selection(base, authorized_source=authorization) == "native-A"
    assert (
        reconcile_primary_source_selection(
            PrimarySourceSelection(
                source_ref="native-A",
                native_id=None,
                operation="resume",
                harness=None,
                runtime_root=tmp_path,
            ),
            authorized_source=authorization,
            resolved_id="native-A",
            resolved_harness="h1",
        )
        == "native-A"
    )

    for changed in (
        PrimarySourceSelection("native-A", "native-B", "resume", "h1", tmp_path),
        PrimarySourceSelection(" ", "native-A", "resume", "h1", tmp_path),
        PrimarySourceSelection("native-A", "native-A", "resume", "h2", tmp_path),
    ):
        with pytest.raises(ValueError, match="source selection conflict"):
            reconcile_primary_source_selection(changed, authorized_source=authorization)

    with pytest.raises(ValueError, match="source selection conflict"):
        reconcile_primary_source_selection(
            base, authorized_source=authorization, resolved_id="native-B"
        )
    with pytest.raises(ValueError, match="source selection conflict"):
        reconcile_primary_source_selection(
            base, authorized_source=authorization, resolved_tracked=True
        )
    with pytest.raises(ValueError, match="source selection conflict"):
        reconcile_primary_source_selection(
            base, authorized_source=authorization, resolved_harness="h2"
        )


def test_fresh_generation_has_no_source_or_seed_but_allows_create_target_elsewhere(
    tmp_path: Path,
) -> None:
    selection = PrimarySourceSelection(None, None, "fresh", "h1", tmp_path)
    assert reconcile_primary_source_selection(selection) is None

    with pytest.raises(ValueError, match="source selection conflict"):
        reconcile_primary_source_selection(
            PrimarySourceSelection(None, "native-A", "fresh", "h1", tmp_path)
        )


def test_alias_native_id_is_compared_only_after_resolution(tmp_path: Path) -> None:
    selection = PrimarySourceSelection("c12", "native-A", "resume", None, tmp_path)
    assert reconcile_primary_source_selection(selection, resolved_id="native-A") == "native-A"
    with pytest.raises(ValueError, match="source selection conflict"):
        reconcile_primary_source_selection(selection, resolved_id="native-B")


@pytest.mark.parametrize("dropped_id", [None, "", " "])
def test_supplied_resolver_must_retain_nonempty_selected_id(
    tmp_path: Path, dropped_id: str | None
) -> None:
    selection = PrimarySourceSelection("native-A", None, "resume", "h1", tmp_path)
    with pytest.raises(ValueError, match="resolver dropped"):
        reconcile_primary_source_selection(
            selection, resolved_id=dropped_id, resolved_id_supplied=True
        )


def test_resolver_snapshot_harness_and_tracked_claim_are_independent_facts(
    tmp_path: Path,
) -> None:
    selection = PrimarySourceSelection(
        "native-A", "native-A", "resume", "h1", tmp_path, tracked_claim=True
    )
    with pytest.raises(ValueError, match="tracked source"):
        reconcile_primary_source_selection(selection)

    selection = PrimarySourceSelection("native-A", "native-A", "fork", "h1", tmp_path)
    with pytest.raises(ValueError, match="harness changed"):
        reconcile_primary_source_selection(
            selection,
            resolved_id="native-A",
            resolved_id_supplied=True,
            resolved_snapshot_harness="h2",
        )


def test_supplied_replay_and_prepared_views_retain_source_and_operation_facts(
    tmp_path: Path,
) -> None:
    original = PrimarySourceSelection("c12", "native-A", "resume", "h1", tmp_path)
    with pytest.raises(ValueError, match="resolver dropped"):
        reconcile_primary_source_selection(
            original, resolved_id=None, resolved_id_supplied=True
        )
    with pytest.raises(ValueError, match="original source reference changed"):
        reconcile_primary_source_selection(
            original, resolved_source_ref=None, resolved_source_ref_supplied=True
        )
    with pytest.raises(ValueError, match="conflicting resolved operation facts"):
        reconcile_primary_source_selection(
            original,
            resolved_operation_facts=("resume", "fork"),
        )
    with pytest.raises(ValueError, match="source operation changed"):
        reconcile_primary_source_selection(
            original,
            resolved_operation_facts=("fork",),
        )
    with pytest.raises(ValueError, match="source selection conflict"):
        reconcile_primary_source_selection(original, resolved_tracked=True)
