from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.launch.composition import build_inline_file_contributions, build_reference_routing
from meridian.lib.launch.reference import (
    ReferenceItem,
    measure_rendered_reference_block_bytes,
    validate_reference_paths,
)


def test_validate_reference_paths_resolves_relative_from_reference_anchor(tmp_path: Path) -> None:
    authority_root = tmp_path / "authority"
    task_cwd = tmp_path / "task"
    kb_dir = authority_root / ".meridian" / "kb"
    authority_root.mkdir(parents=True, exist_ok=True)
    task_cwd.mkdir(parents=True, exist_ok=True)
    kb_dir.mkdir(parents=True, exist_ok=True)
    target = task_cwd / "notes.md"
    target.write_text("hello", encoding="utf-8")

    resolved = validate_reference_paths(
        ("notes.md",),
        reference_anchor=task_cwd,
        kb_dir=kb_dir,
    )

    assert resolved == (target.resolve(),)


def test_validate_reference_paths_kb_prefix_confined_to_kb_dir(tmp_path: Path) -> None:
    kb_dir = tmp_path / "authority" / ".meridian" / "kb"
    anchor = tmp_path / "task"
    kb_dir.mkdir(parents=True, exist_ok=True)
    anchor.mkdir(parents=True, exist_ok=True)
    kb_file = kb_dir / "domain" / "decision.md"
    kb_file.parent.mkdir(parents=True, exist_ok=True)
    kb_file.write_text("decision", encoding="utf-8")

    resolved = validate_reference_paths(
        ("kb:domain/decision.md",),
        reference_anchor=anchor,
        kb_dir=kb_dir,
    )

    assert resolved == (kb_file.resolve(),)


def test_validate_reference_paths_kb_prefix_uses_authority_kb_not_reference_anchor(
    tmp_path: Path,
) -> None:
    authority_kb_dir = tmp_path / "authority" / ".meridian" / "kb"
    task_anchor = tmp_path / "task"
    authority_kb_dir.mkdir(parents=True, exist_ok=True)
    task_anchor.mkdir(parents=True, exist_ok=True)

    authority_file = authority_kb_dir / "domain" / "decision.md"
    authority_file.parent.mkdir(parents=True, exist_ok=True)
    authority_file.write_text("authority", encoding="utf-8")

    shadow_kb_file = task_anchor / ".meridian" / "kb" / "domain" / "decision.md"
    shadow_kb_file.parent.mkdir(parents=True, exist_ok=True)
    shadow_kb_file.write_text("task-shadow", encoding="utf-8")

    resolved = validate_reference_paths(
        ("kb:domain/decision.md",),
        reference_anchor=task_anchor,
        kb_dir=authority_kb_dir,
    )

    assert resolved == (authority_file.resolve(),)


def test_validate_reference_paths_keeps_absolute_path(tmp_path: Path) -> None:
    kb_dir = tmp_path / "authority" / ".meridian" / "kb"
    anchor = tmp_path / "task"
    kb_dir.mkdir(parents=True, exist_ok=True)
    anchor.mkdir(parents=True, exist_ok=True)
    absolute_file = tmp_path / "elsewhere" / "absolute.md"
    absolute_file.parent.mkdir(parents=True, exist_ok=True)
    absolute_file.write_text("absolute", encoding="utf-8")

    resolved = validate_reference_paths(
        (absolute_file,),
        reference_anchor=anchor,
        kb_dir=kb_dir,
    )

    assert resolved == (absolute_file.resolve(),)


def test_validate_reference_paths_expands_home_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kb_dir = tmp_path / "authority" / ".meridian" / "kb"
    anchor = tmp_path / "task"
    home = tmp_path / "home"
    kb_dir.mkdir(parents=True, exist_ok=True)
    anchor.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    home_file = home / "ref.md"
    home_file.write_text("home", encoding="utf-8")
    monkeypatch.setenv("HOME", home.as_posix())
    monkeypatch.setenv("USERPROFILE", home.as_posix())

    resolved = validate_reference_paths(
        ("~/ref.md",),
        reference_anchor=anchor,
        kb_dir=kb_dir,
    )

    assert resolved == (home_file.resolve(),)


@pytest.mark.parametrize("raw", ["kb:", "kb:/absolute.md", "kb:../escape.md"])
def test_validate_reference_paths_rejects_invalid_kb_prefix(raw: str, tmp_path: Path) -> None:
    kb_dir = tmp_path / "authority" / ".meridian" / "kb"
    anchor = tmp_path / "task"
    kb_dir.mkdir(parents=True, exist_ok=True)
    anchor.mkdir(parents=True, exist_ok=True)

    with pytest.raises(ValueError):
        validate_reference_paths((raw,), reference_anchor=anchor, kb_dir=kb_dir)


def test_validate_reference_paths_rejects_at_prefix(tmp_path: Path) -> None:
    kb_dir = tmp_path / "authority" / ".meridian" / "kb"
    anchor = tmp_path / "task"
    kb_dir.mkdir(parents=True, exist_ok=True)
    anchor.mkdir(parents=True, exist_ok=True)

    with pytest.raises(ValueError) as exc_info:
        validate_reference_paths(("@domain/page.md",), reference_anchor=anchor, kb_dir=kb_dir)

    assert "no longer supported" in str(exc_info.value)
    assert "kb:domain/page.md" in str(exc_info.value)


def test_validate_reference_paths_missing_relative_reports_anchor_and_resolved_path(
    tmp_path: Path,
) -> None:
    kb_dir = tmp_path / "authority" / ".meridian" / "kb"
    anchor = tmp_path / "task"
    kb_dir.mkdir(parents=True, exist_ok=True)
    anchor.mkdir(parents=True, exist_ok=True)

    with pytest.raises(FileNotFoundError) as exc_info:
        validate_reference_paths(("missing.md",), reference_anchor=anchor, kb_dir=kb_dir)

    message = str(exc_info.value)
    assert "Given: missing.md" in message
    assert f"Reference anchor: {anchor.resolve()}" in message
    assert f"Resolved path: {(anchor / 'missing.md').resolve()}" in message


def test_measure_rendered_reference_block_bytes_counts_rendered_header_and_body() -> None:
    reference = ReferenceItem(
        kind="file",
        path=Path("docs/reference.md"),
        body="alpha\n",
    )

    assert measure_rendered_reference_block_bytes(reference) == len(
        b"# Reference: docs/reference.md\n\nalpha"
    )


def test_measure_rendered_reference_block_bytes_can_exclude_warning_only_refs() -> None:
    reference = ReferenceItem(
        kind="file",
        path=Path("binary.bin"),
        body="",
        warning="binary file skipped",
    )

    assert measure_rendered_reference_block_bytes(reference) == len(
        b"# Reference: binary.bin\n\n[binary file skipped]"
    )
    assert measure_rendered_reference_block_bytes(
        reference, include_warning_only=False
    ) == 0


def test_build_inline_file_contributions_excludes_warning_only_and_omitted_refs() -> None:
    content_ref = ReferenceItem(
        kind="file",
        path=Path("notes/long.md"),
        body="This is a longer body.\n",
    )
    warning_ref = ReferenceItem(
        kind="file",
        path=Path("docs/binary.bin"),
        body="",
        warning="binary file skipped",
    )
    directory_ref = ReferenceItem(
        kind="directory",
        path=Path("docs"),
        body="tree output",
    )
    omitted_ref = ReferenceItem(kind="file", path=Path("empty.txt"), body="")

    reference_items = (
        content_ref,
        warning_ref,
        directory_ref,
        omitted_ref,
    )
    reference_routing = build_reference_routing(reference_items)

    contributions = build_inline_file_contributions(
        reference_items,
        reference_routing,
        exclude_warning_only=True,
    )

    assert [contribution.path for contribution in contributions] == ["notes/long.md"]
    assert contributions[0].byte_count == len(
        b"# Reference: notes/long.md\n\nThis is a longer body."
    )
