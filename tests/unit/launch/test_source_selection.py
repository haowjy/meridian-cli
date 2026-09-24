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
