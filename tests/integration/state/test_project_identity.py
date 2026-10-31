from __future__ import annotations

import hashlib
import json
import os
import time
import tomllib
from pathlib import Path

import pytest

from meridian.lib.config.project_root import has_project_marker
from meridian.lib.state.project_identity import (
    AdHoc,
    Committed,
    ensure_project_identity,
    mint_committed_identity,
    nearest_ancestor_project_marker,
    resolve_project_identity,
    state_id,
)


def test_project_predicate_recognizes_markers_except_user_mars_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    user_mars_root = home / "mars"
    user_mars_root.mkdir(parents=True)
    monkeypatch.setenv("MERIDIAN_HOME", home.as_posix())

    assert not has_project_marker(tmp_path)
    (tmp_path / "mars.toml").write_text("[settings]\n", encoding="utf-8")
    assert has_project_marker(tmp_path)
    (user_mars_root / "mars.toml").write_text("[settings]\n", encoding="utf-8")
    assert not has_project_marker(user_mars_root)
    assert isinstance(resolve_project_identity(user_mars_root), AdHoc)


def test_nearest_ancestor_marker_is_notice_only(tmp_path: Path) -> None:
    ancestor = tmp_path / "project"
    child = ancestor / "nested" / "scratch"
    child.mkdir(parents=True)
    (ancestor / "meridian.toml").write_text("", encoding="utf-8")

    assert nearest_ancestor_project_marker(child) == ancestor
    assert isinstance(resolve_project_identity(child), AdHoc)


def test_bare_identity_read_is_pure_and_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    project_root = tmp_path / "My Scratch!"
    project_root.mkdir()
    monkeypatch.setenv("MERIDIAN_HOME", home.as_posix())

    identity = resolve_project_identity(project_root)

    digest = hashlib.sha256(project_root.resolve().as_posix().encode()).hexdigest()[:12]
    assert identity == AdHoc(id=f"adhoc.my-scratch.{digest}", materialized=False)
    assert not home.exists()
    assert list(project_root.iterdir()) == []
    assert state_id(identity) is None


def test_ensure_adhoc_identity_writes_origin_only_under_user_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    project_root = tmp_path / "scratch"
    project_root.mkdir()
    monkeypatch.setenv("MERIDIAN_HOME", home.as_posix())

    identity = ensure_project_identity(project_root)

    assert isinstance(identity, AdHoc)
    assert identity.materialized
    assert state_id(identity) == identity.id
    origin_path = home / "projects" / identity.id / "origin.json"
    original_bytes = origin_path.read_bytes()
    original_mtime = origin_path.stat().st_mtime_ns
    origin = json.loads(origin_path.read_text(encoding="utf-8"))
    assert set(origin) == {"path", "created_at"}
    assert origin["path"] == project_root.resolve().as_posix()
    assert list(project_root.iterdir()) == []
    assert resolve_project_identity(project_root) == identity
    assert ensure_project_identity(project_root) == identity
    assert origin_path.read_bytes() == original_bytes
    assert origin_path.stat().st_mtime_ns == original_mtime

    stale_time = time.time() - 3700
    os.utime(origin_path, (stale_time, stale_time))
    ensure_project_identity(project_root)
    assert origin_path.stat().st_mtime > stale_time
    assert origin_path.read_bytes() == original_bytes


def test_committed_identity_is_read_without_minting(tmp_path: Path) -> None:
    (tmp_path / "meridian.toml").write_text('[project]\nid = "shared-clone-id"\n', encoding="utf-8")

    assert resolve_project_identity(tmp_path) == Committed(id="shared-clone-id")
    assert ensure_project_identity(tmp_path) == Committed(id="shared-clone-id")


def test_mars_only_write_mints_committed_identity(tmp_path: Path) -> None:
    (tmp_path / "mars.toml").write_text("[settings]\n", encoding="utf-8")

    identity = ensure_project_identity(tmp_path)

    assert isinstance(identity, Committed)
    assert identity.id is not None
    payload = tomllib.loads((tmp_path / "meridian.toml").read_text(encoding="utf-8"))
    assert payload["project"]["id"] == identity.id


def test_scaffold_and_committed_identity_are_written_together(tmp_path: Path) -> None:
    identity = mint_committed_identity(
        tmp_path,
        "[defaults]\nmax_depth = 7\n",
    )

    assert isinstance(identity, Committed)
    payload = tomllib.loads((tmp_path / "meridian.toml").read_text(encoding="utf-8"))
    assert payload["defaults"]["max_depth"] == 7
    assert payload["project"]["id"] == identity.id


def test_ensure_committed_identity_preserves_existing_content_verbatim(
    tmp_path: Path,
) -> None:
    original = "# user formatting\n[defaults]\nmax_depth=7 # keep this\n"
    config_path = tmp_path / "meridian.toml"
    config_path.write_text(original, encoding="utf-8")

    identity = ensure_project_identity(tmp_path)

    assert isinstance(identity, Committed)
    assert identity.id is not None
    updated = config_path.read_text(encoding="utf-8")
    assert updated.startswith(original)
    assert updated.removeprefix(original) == (
        f'\n# managed by meridian — do not edit\n[project]\nid = "{identity.id}"\n'
    )


def test_failed_atomic_identity_write_leaves_existing_toml_usable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = "[defaults]\nmax_depth = 4\n"
    config_path = tmp_path / "meridian.toml"
    config_path.write_text(original, encoding="utf-8")

    def interrupted(_path: Path, _content: str) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        "meridian.lib.state.project_identity.atomic_write_text",
        interrupted,
    )
    with pytest.raises(KeyboardInterrupt):
        ensure_project_identity(tmp_path)

    assert config_path.read_text(encoding="utf-8") == original
    assert tomllib.loads(config_path.read_text(encoding="utf-8"))["defaults"]["max_depth"] == 4


def test_committed_identity_rejects_reserved_adhoc_prefix(tmp_path: Path) -> None:
    (tmp_path / "meridian.toml").write_text(
        '[project]\nid = "adhoc.repo.0123456789ab"\n', encoding="utf-8"
    )

    with pytest.raises(ValueError, match="reserved"):
        resolve_project_identity(tmp_path)


@pytest.mark.parametrize(
    "content",
    ['project = "not-a-table"\n', "[project]\nid = 42\n"],
)
def test_committed_identity_rejects_invalid_project_shape(
    tmp_path: Path,
    content: str,
) -> None:
    (tmp_path / "meridian.toml").write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match=r"Invalid \[project\]"):
        resolve_project_identity(tmp_path)


def test_legacy_dot_meridian_id_is_not_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    legacy = tmp_path / ".meridian"
    legacy.mkdir()
    (legacy / "id").write_text("legacy-project-id\n", encoding="utf-8")
    monkeypatch.setenv("MERIDIAN_HOME", home.as_posix())

    identity = resolve_project_identity(tmp_path)

    assert isinstance(identity, AdHoc)
    assert identity.id != "legacy-project-id"
