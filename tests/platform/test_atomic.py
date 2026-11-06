import os
import stat
from pathlib import Path

import pytest

from meridian.lib.platform import atomic as platform_atomic
from meridian.lib.state import atomic as atomic_module
from tests.conftest import posix_only


def _tmp_candidates(path: Path) -> list[Path]:
    return list(path.parent.glob(f".{path.name}.*.tmp"))


def test_atomic_replace_exception_preserves_old_content(tmp_path: Path) -> None:
    target = tmp_path / "state.txt"
    target.write_text("before\n", encoding="utf-8")

    with (
        pytest.raises(RuntimeError, match="simulated crash"),
        platform_atomic.atomic_replace(target) as handle,
    ):
        handle.write("partial")
        raise RuntimeError("simulated crash")

    assert target.read_text(encoding="utf-8") == "before\n"
    assert _tmp_candidates(target) == []


@posix_only
def test_atomic_replace_new_file_mode_honors_umask(tmp_path: Path) -> None:
    target = tmp_path / "new.txt"
    previous_umask = os.umask(0o027)
    try:
        with platform_atomic.atomic_replace(target) as handle:
            handle.write("new\n")
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_atomic_replace_reports_post_commit_directory_fsync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "state.txt"
    target.write_text("before\n", encoding="utf-8")

    def fail_directory_sync(_path: Path) -> None:
        raise OSError("directory sync failed")

    monkeypatch.setattr(platform_atomic, "fsync_directory", fail_directory_sync)

    with (
        pytest.raises(platform_atomic.AtomicReplaceDurabilityError) as raised,
        platform_atomic.atomic_replace(target) as handle,
    ):
        handle.write("after\n")

    assert raised.value.path == target
    assert raised.value.committed is True
    assert target.read_text(encoding="utf-8") == "after\n"


def test_atomic_publish_dir_rejects_existing_destination(tmp_path: Path) -> None:
    stage_dir = tmp_path / ".staging" / "p1-1234-deadbeef"
    dest_dir = tmp_path / "p1"
    stage_dir.mkdir(parents=True)
    dest_dir.mkdir()
    (stage_dir / "state.json").write_text("staged\n", encoding="utf-8")
    (dest_dir / "state.json").write_text("existing\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="Refusing to publish over existing destination"):
        atomic_module.atomic_publish_dir(stage_dir, dest_dir)

    assert (stage_dir / "state.json").read_text(encoding="utf-8") == "staged\n"
    assert (dest_dir / "state.json").read_text(encoding="utf-8") == "existing\n"
