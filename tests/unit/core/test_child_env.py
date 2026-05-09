"""Unit tests for the shared child-env contract."""

from pathlib import Path

import pytest

from meridian.lib.core.child_env import (
    ALLOWED_CHILD_ENV_KEYS,
    validate_child_env_keys,
)
from meridian.lib.core.resolved_context import ResolvedContext
from meridian.lib.core.types import SpawnId

# ---------------------------------------------------------------------------
# validate_child_env_keys
# ---------------------------------------------------------------------------


def test_validate_accepts_all_allowed_keys() -> None:
    """All keys in ALLOWED_CHILD_ENV_KEYS must pass validation without error."""
    overrides = {key: "value" for key in ALLOWED_CHILD_ENV_KEYS}
    # Should not raise
    validate_child_env_keys(overrides)


def test_validate_accepts_non_meridian_keys() -> None:
    """Non-MERIDIAN_* keys must always pass validation."""
    overrides = {
        "PATH": "/usr/bin",
        "HOME": "/home/user",
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "80",
    }
    validate_child_env_keys(overrides)


def test_validate_accepts_context_dir_keys() -> None:
    validate_child_env_keys({"MERIDIAN_CONTEXT_DOCS_DIR": "/contexts/docs"})


def test_validate_rejects_unexpected_meridian_key() -> None:
    """An unknown MERIDIAN_* key must raise RuntimeError."""
    overrides = {"MERIDIAN_UNKNOWN_CUSTOM": "value"}
    with pytest.raises(RuntimeError, match="Unexpected MERIDIAN_\\* key in child env"):
        validate_child_env_keys(overrides)


def test_validate_rejects_unexpected_key_mixed_with_allowed() -> None:
    """RuntimeError must be raised even when some allowed keys are present."""
    overrides = {
        "MERIDIAN_DEPTH": "1",
        "MERIDIAN_PROJECT_DIR": "/repo",
        "MERIDIAN_NOVEL_KEY": "bad",
    }
    with pytest.raises(RuntimeError, match="MERIDIAN_NOVEL_KEY"):
        validate_child_env_keys(overrides)


def test_validate_rejects_context_dir_near_misses() -> None:
    for key in (
        "MERIDIAN_CONTEXT_DOCS",
        "MERIDIAN_CONTEXT__DIR",
        "MERIDIAN_CONTEXT_DOCS_DIR_EXTRA",
    ):
        with pytest.raises(RuntimeError, match=key):
            validate_child_env_keys({key: "/bad"})


# ---------------------------------------------------------------------------
# ResolvedContext.child_env_overrides
# ---------------------------------------------------------------------------


def test_child_env_overrides_produces_depth_always() -> None:
    """MERIDIAN_DEPTH must always appear in the result."""
    result = ResolvedContext(depth=0).child_env_overrides()
    assert "MERIDIAN_DEPTH" in result


def test_child_env_overrides_increments_depth_by_default() -> None:
    """Default increment_depth=True must produce depth + 1."""
    result = ResolvedContext(depth=3).child_env_overrides()
    assert result["MERIDIAN_DEPTH"] == "4"


def test_child_env_overrides_omits_none_fields() -> None:
    """Fields that are None/empty must not appear in the result dict."""
    result = ResolvedContext(depth=0).child_env_overrides()
    assert "MERIDIAN_PROJECT_DIR" not in result
    assert "MERIDIAN_RUNTIME_DIR" not in result
    assert "MERIDIAN_CHAT_ID" not in result
    assert "MERIDIAN_ACTIVE_WORK_ID" not in result


def test_child_env_overrides_full_overrides() -> None:
    """All populated fields must appear with correct string values."""
    repo = Path("/repo")
    state = Path("/runtime/state")
    result = ResolvedContext(
        depth=1,
        project_root=repo,
        runtime_root=state,
        chat_id="c99",
        work_id="w1",
        work_dir=Path("/repo/.meridian/work/w1"),
        context_dirs=(
            ("work", Path("/repo/.meridian/work")),
            ("work_archive", Path("/repo/.meridian/archive/work")),
            ("kb", Path("/repo/.meridian/kb")),
        ),
    ).child_env_overrides()

    assert result == {
        "MERIDIAN_DEPTH": "2",
        "MERIDIAN_PROJECT_DIR": "/repo",
        "MERIDIAN_RUNTIME_DIR": "/runtime/state",
        "MERIDIAN_CHAT_ID": "c99",
        "MERIDIAN_ACTIVE_WORK_ID": "w1",
        "MERIDIAN_ACTIVE_WORK_DIR": "/repo/.meridian/work/w1",
        "MERIDIAN_CONTEXT_WORK_DIR": "/repo/.meridian/work",
        "MERIDIAN_CONTEXT_WORK_ARCHIVE_DIR": "/repo/.meridian/archive/work",
        "MERIDIAN_CONTEXT_KB_DIR": "/repo/.meridian/kb",
    }


def test_child_env_overrides_with_child_spawn_id() -> None:
    result = ResolvedContext(
        spawn_id=SpawnId("p-parent"),
        depth=1,
    ).child_env_overrides(child_spawn_id="p-child")

    assert result["MERIDIAN_SPAWN_ID"] == "p-child"
    assert result["MERIDIAN_PARENT_SPAWN_ID"] == "p-parent"


def test_child_env_overrides_with_context_dirs() -> None:
    result = ResolvedContext(
        depth=0,
        context_dirs=(("docs", Path("/contexts/docs")),),
    ).child_env_overrides()

    assert result["MERIDIAN_CONTEXT_DOCS_DIR"] == "/contexts/docs"


def test_child_env_overrides_result_keys_are_subset_of_allowed() -> None:
    """All keys produced by child_env_overrides must be in ALLOWED_CHILD_ENV_KEYS."""
    result = ResolvedContext(
        depth=0,
        project_root=Path("/r"),
        runtime_root=Path("/s"),
        chat_id="c1",
        work_id="wid",
        work_dir=Path("/s/work/wid"),
    ).child_env_overrides()
    unexpected = set(result) - ALLOWED_CHILD_ENV_KEYS
    assert unexpected == set(), f"Unexpected keys: {unexpected}"
