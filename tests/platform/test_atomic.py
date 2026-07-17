from pathlib import Path

import pytest

from meridian.lib.platform import atomic as platform_atomic
from meridian.lib.state import atomic as atomic_module
from tests.conftest import posix_only


def _tmp_candidates(path: Path) -> list[Path]:
    return list(path.parent.glob(f".{path.name}.*.tmp"))


def _capture_fsync_calls(
    monkeypatch: pytest.MonkeyPatch,
    *,
    directory_path: Path | None = None,
    directory_fd: int | None = None,
) -> list[int]:
    fsync_calls: list[int] = []
    original_open = platform_atomic.os.open
    original_close = platform_atomic.os.close

    monkeypatch.setattr(platform_atomic.os, "fsync", fsync_calls.append)
    if directory_fd is None:
        return fsync_calls

    assert directory_path is not None

    def fake_open(path: str | Path, flags: int, mode: int = 0o777) -> int:
        if Path(path) == directory_path:
            return directory_fd
        return original_open(path, flags, mode)

    def fake_close(fd: int) -> None:
        if fd == directory_fd:
            return
        original_close(fd)

    monkeypatch.setattr(platform_atomic.os, "open", fake_open)
    monkeypatch.setattr(platform_atomic.os, "close", fake_close)
    return fsync_calls


@posix_only
def test_atomic_write_text_fsyncs_and_replaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "state.txt"
    target.write_text("before\n", encoding="utf-8")
    directory_fd = 999_001
    fsync_calls = _capture_fsync_calls(
        monkeypatch,
        directory_path=target.parent,
        directory_fd=directory_fd,
    )

    atomic_module.atomic_write_text(target, "after\n")

    assert target.read_text(encoding="utf-8") == "after\n"
    assert directory_fd in fsync_calls
    assert _tmp_candidates(target) == []


@posix_only
def test_atomic_write_bytes_fsyncs_and_replaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "state.bin"
    target.write_bytes(b"before")
    directory_fd = 999_002
    fsync_calls = _capture_fsync_calls(
        monkeypatch,
        directory_path=target.parent,
        directory_fd=directory_fd,
    )

    atomic_module.atomic_write_bytes(target, b"after")

    assert target.read_bytes() == b"after"
    assert directory_fd in fsync_calls
    assert _tmp_candidates(target) == []


@posix_only
def test_append_text_line_fsyncs_new_file_directory_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "events.jsonl"
    directory_fd = 999_003
    fsync_calls = _capture_fsync_calls(
        monkeypatch,
        directory_path=target.parent,
        directory_fd=directory_fd,
    )

    atomic_module.append_text_line(target, '{"event":"start"}\n')

    assert target.read_text(encoding="utf-8") == '{"event":"start"}\n'
    assert directory_fd in fsync_calls


@posix_only
def test_append_text_line_skips_directory_fsync_when_file_already_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "events.jsonl"
    target.write_text("", encoding="utf-8")
    directory_fd = 999_004
    fsync_calls = _capture_fsync_calls(
        monkeypatch,
        directory_path=target.parent,
        directory_fd=directory_fd,
    )

    atomic_module.append_text_line(target, '{"event":"resume"}\n')

    assert target.read_text(encoding="utf-8") == '{"event":"resume"}\n'
    assert directory_fd not in fsync_calls


def test_atomic_write_text_replaces_content_cross_platform(tmp_path: Path) -> None:
    target = tmp_path / "state.txt"
    target.write_text("before\n", encoding="utf-8")

    atomic_module.atomic_write_text(target, "after\n")

    assert target.read_text(encoding="utf-8") == "after\n"
    assert _tmp_candidates(target) == []


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
def test_atomic_publish_dir_replaces_entry_and_fsyncs_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawns_dir = tmp_path / "spawns"
    stage_dir = spawns_dir / ".staging" / "p1-1234-deadbeef"
    dest_dir = spawns_dir / "p1"
    stage_dir.mkdir(parents=True)
    (stage_dir / "state.json").write_text("complete\n", encoding="utf-8")
    directory_fd = 999_005
    fsync_calls = _capture_fsync_calls(
        monkeypatch,
        directory_path=spawns_dir,
        directory_fd=directory_fd,
    )

    atomic_module.atomic_publish_dir(stage_dir, dest_dir)

    assert not stage_dir.exists()
    assert (dest_dir / "state.json").read_text(encoding="utf-8") == "complete\n"
    assert directory_fd in fsync_calls


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
