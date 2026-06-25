from pathlib import Path

import pytest

from meridian.lib.ops.spawn.context_ref import resolve_context_ref


def test_resolve_context_ref_rejects_filesystem_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="--from does not accept filesystem paths"):
        resolve_context_ref(tmp_path, "./notes/context.md")

    with pytest.raises(ValueError, match="use --file/-f instead"):
        resolve_context_ref(tmp_path, "/tmp/report.md")


def test_resolve_context_ref_still_accepts_spawn_and_session_refs(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Spawn 'p999' not found"):
        resolve_context_ref(tmp_path, "p999")

    with pytest.raises(ValueError, match="No primary spawn found"):
        resolve_context_ref(tmp_path, "c999")
