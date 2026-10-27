#!/usr/bin/env python3
"""Validate, render, and extract changelog notes authored in a PR body.

Invariant: a PR never edits CHANGELOG.md. Its `## Changelog` section is the
entry; the release job renders that section into CHANGELOG.md under the new
version heading and reuses the text as the GitHub Release body. One writer
(the release job) is what keeps two branches from conflicting on the file.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import os
import re
import stat
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass

_SECTION_ORDER = (
    "Upgrade notes",
    "Added",
    "Changed",
    "Deprecated",
    "Removed",
    "Fixed",
    "Security",
)
_SECTION_LIST = ", ".join(_SECTION_ORDER)
# The PR template's own `##` sections. One may open with a bullet (`## Why`
# sometimes does); that bullet is not changelog text dropped by a stray heading.
_PROSE_SECTION_HEADINGS = frozenset(
    {
        "Why",
        "Core design decisions",
        "Goal",
        "Work Item",
        "Summary",
        "Diff",
        "Before / After",
        "Code Changes",
        "State / Schema Changes",
        "Testing",
        "Verification",
        "Deferred",
        "Knowledge Updates",
        "Spawn Trace",
        "Release Label Guide",
        "Post-Merge Automation",
        "Cleanup",
    }
)
_CHANGELOG_PATH = "CHANGELOG.md"
_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CHANGELOG_HEADING_RE = re.compile(r"^##\s+Changelog\s*$")
_ANY_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_TOP_HEADING_RE = re.compile(r"^#{1,2}\s+")
_RELEASE_HEADING_RE = re.compile(r"^##\s+\[([^\]]+)\]")
_UNRELEASED_RE = re.compile(r"^##\s+\[Unreleased\]\s*$")
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})")
_INLINE_CODE_RE = re.compile(r"(`+).*?\1")
# A multi-word angle-bracket token is template prose; a single-word `<id>` or
# `<path>` is a metavariable. Requiring non-space at both inner edges keeps
# comparison prose such as `a < b and c > d` out.
_PLACEHOLDER_RE = re.compile(
    r"<(?:description|placeholder|\.{2,})>"
    r"|<(?=[^<>\n]*\s)[^\s<>](?:[^<>\n]*[^\s<>])?>",
    re.IGNORECASE,
)
_PLACEHOLDER_ONLY_RE = re.compile(r"<[^<>\n]+>|TODO|TBD", re.IGNORECASE)
_NONE_RE = re.compile(r"^None\.\s+\S")
_PR_NOTES_RE = re.compile(r"(?:^|/)notes-\d+-pr(\d+)\.md$")


class ChangelogError(RuntimeError):
    """Raised when a file cannot be read, rendered, or extracted."""


@dataclass(frozen=True)
class Section:
    name: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class Notes:
    sections: tuple[Section, ...]
    none_line: str | None
    errors: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class ScanIssue:
    line: int
    message: str


def _scan(lines: Sequence[str]) -> tuple[list[str], list[str], ScanIssue | None]:
    """Return (visible, clean) line views plus an unterminated-block issue.

    `visible` blanks fenced code blocks, inline code spans, and HTML comments,
    so structure and placeholders are read from prose only. `clean` keeps
    fenced and inline code (a changelog entry may carry an example) and strips
    comments, so rendering never copies guidance comments into CHANGELOG.md.
    """
    visible: list[str] = []
    clean: list[str] = []
    fence: str | None = None
    fence_line = 0
    in_comment = False
    comment_line = 0

    for number, line in enumerate(lines, start=1):
        stripped = line.lstrip()
        if fence is not None:
            if (
                stripped.rstrip()
                and set(stripped.rstrip()) == {fence[0]}
                and len(stripped.rstrip()) >= len(fence)
            ):
                fence = None
            visible.append("")
            clean.append(line.rstrip())
            continue

        if in_comment:
            end = line.find("-->")
            if end == -1:
                visible.append("")
                clean.append("")
                continue
            prose = line[end + 3 :]
            in_comment = False
        else:
            fence_match = _FENCE_RE.match(stripped)
            if fence_match is not None:
                fence = fence_match.group(1)
                fence_line = number
                visible.append("")
                clean.append(line.rstrip())
                continue
            prose = line

        while True:
            start = prose.find("<!--")
            if start == -1:
                break
            end = prose.find("-->", start + 4)
            if end == -1:
                prose = prose[:start]
                in_comment = True
                comment_line = number
                break
            prose = prose[:start] + prose[end + 3 :]

        visible.append(_INLINE_CODE_RE.sub(lambda match: "x" * len(match.group(0)), prose.rstrip()))
        clean.append(prose.rstrip())

    issue: ScanIssue | None = None
    if fence is not None:
        issue = ScanIssue(fence_line, "unclosed fenced code block; add a closing fence")
    elif in_comment:
        issue = ScanIssue(comment_line, "unclosed HTML comment; add '-->'")
    return visible, clean, issue


def _error(source: str, line: int | None, message: str) -> str:
    if line is None:
        return f"{source}: {message}"
    return f"{source}:{line}: {message}"


def _notes_source(path: str) -> str:
    if path == "-":
        return "stdin"
    match = _PR_NOTES_RE.search(path)
    if match is not None:
        return f"{path} (PR #{match.group(1)})"
    return path


def parse_notes(text: str, source: str = "PR body") -> Notes:
    """Parse the `## Changelog` section of a notes file, collecting every error."""
    lines = text.splitlines()
    visible, clean, issue = _scan(lines)
    errors: list[str] = []
    if issue is not None:
        errors.append(_error(source, issue.line, issue.message))
        return Notes(sections=(), none_line=None, errors=tuple(errors))

    heading_lines = [
        index for index, line in enumerate(visible) if _CHANGELOG_HEADING_RE.match(line)
    ]
    if not heading_lines:
        errors.append(
            _error(
                source,
                None,
                "no '## Changelog' heading; the heading must be exactly "
                "'## Changelog' (an unclosed '<!--' also hides everything after it)",
            )
        )
        return Notes(sections=(), none_line=None, errors=tuple(errors))
    if len(heading_lines) > 1:
        for duplicate in heading_lines[1:]:
            errors.append(
                _error(source, duplicate + 1, "second '## Changelog' section; keep exactly one")
            )

    start = heading_lines[0]
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if _TOP_HEADING_RE.match(visible[index])
        ),
        len(lines),
    )
    content = range(start + 1, end)

    if end < len(lines):
        boundary = visible[end].strip()
        block_end = next(
            (
                index
                for index in range(end + 1, len(lines))
                if _TOP_HEADING_RE.match(visible[index])
            ),
            len(lines),
        )
        for index in range(end + 1, block_end):
            heading = _ANY_HEADING_RE.match(visible[index].strip())
            if (
                heading is not None
                and len(heading.group(1)) == 3
                and heading.group(2).strip() in _SECTION_ORDER
            ):
                errors.append(
                    _error(
                        source,
                        index + 1,
                        f"'### {heading.group(2).strip()}' after {boundary!r} is dropped: "
                        "a '##' heading ends the '## Changelog' section; put it under "
                        "'## Changelog'",
                    )
                )
                break
        boundary_heading = _ANY_HEADING_RE.match(boundary)
        prose_section = (
            boundary_heading is not None
            and boundary_heading.group(2).strip() in _PROSE_SECTION_HEADINGS
        )
        dropped_bullet = next(
            (
                index
                for index in range(end + 1, block_end)
                if visible[index].strip().startswith("- ")
            ),
            None,
        )
        if not prose_section and dropped_bullet is not None:
            errors.append(
                _error(
                    source,
                    dropped_bullet + 1,
                    f"bullet after {boundary!r} is dropped: a '##' heading ends the "
                    "'## Changelog' section; move it under '## Changelog'",
                )
            )

    for index in content:
        match = _PLACEHOLDER_RE.search(visible[index])
        if match is not None:
            errors.append(
                _error(source, index + 1, f"leftover placeholder {match.group(0)!r}")
            )

    first = next((index for index in content if visible[index].strip()), None)
    if first is None:
        errors.append(
            _error(
                source,
                start + 1,
                "'## Changelog' is empty; put 'None. <reason>' or '### <Section>' "
                "blocks under it (a '#' or '##' heading ends the section)",
            )
        )
        return Notes(sections=(), none_line=None, errors=tuple(errors))

    first_text = visible[first].strip()
    if first_text.startswith("None."):
        none_line = clean[first].strip()
        if not _NONE_RE.match(none_line):
            errors.append(
                _error(
                    source,
                    first + 1,
                    "'None.' needs a reason, for example 'None. Internal refactor only.'",
                )
            )
        reason = none_line[len("None.") :].strip()
        if _PLACEHOLDER_ONLY_RE.fullmatch(reason):
            errors.append(_error(source, first + 1, "leftover placeholder in the 'None.' reason"))
        for index in content:
            if index > first and visible[index].strip():
                errors.append(
                    _error(
                        source,
                        index + 1,
                        "'None. <reason>' must be the only content under '## Changelog'",
                    )
                )
        return Notes(sections=(), none_line=none_line, errors=tuple(errors))

    sections: list[Section] = []
    seen: set[str] = set()
    name: str | None = None
    name_line: int | None = None
    body: list[str] = []
    started = False
    has_bullet = False
    unknown_section = False
    bullet_parts: list[str] = []
    bullet_line: int | None = None

    def finish_bullet() -> None:
        nonlocal bullet_parts, bullet_line
        if bullet_line is None:
            return
        text = " ".join(part for part in bullet_parts if part).strip()
        if _PLACEHOLDER_ONLY_RE.fullmatch(text):
            errors.append(
                _error(source, bullet_line, f"placeholder bullet {text!r}; write what changed")
            )
        bullet_parts = []
        bullet_line = None

    def finish() -> None:
        nonlocal name, name_line, body, started, has_bullet
        if name is None:
            return
        finish_bullet()
        while body and not body[0].strip():
            body.pop(0)
        while body and not body[-1].strip():
            body.pop()
        if not has_bullet:
            errors.append(_error(source, name_line, f"'### {name}' has no '- ' bullet"))
        sections.append(Section(name=name, lines=tuple(body)))
        name = None
        name_line = None
        body = []
        started = False
        has_bullet = False

    for index in content:
        line = visible[index]
        stripped = line.strip()
        if not stripped:
            if name is not None:
                body.append(clean[index])
            continue

        heading = _ANY_HEADING_RE.match(stripped)
        if heading is not None:
            finish()
            level = len(heading.group(1))
            title = heading.group(2).strip()
            if level != 3 or title not in _SECTION_ORDER:
                unknown_section = True
                errors.append(
                    _error(
                        source,
                        index + 1,
                        f"{stripped!r} is not a changelog section; use '### <Section>' "
                        f"with one of: {_SECTION_LIST}",
                    )
                )
                continue
            unknown_section = False
            if title in seen:
                errors.append(_error(source, index + 1, f"duplicate '### {title}' section"))
            seen.add(title)
            name = title
            name_line = index + 1
            continue

        if unknown_section:
            continue

        if name is None:
            errors.append(
                _error(
                    source,
                    index + 1,
                    "expected 'None. <reason>' or '### <Section>', got: "
                    f"{clean[index].strip()!r}",
                )
            )
            continue

        if stripped.startswith("- "):
            finish_bullet()
            bullet_line = index + 1
            bullet_parts.append(stripped[2:].strip())
        elif bullet_line is not None:
            bullet_parts.append(stripped)

        body.append(clean[index])
        if not started:
            started = True
            if not stripped.startswith("- "):
                errors.append(
                    _error(source, index + 1, f"'### {name}' must start with a '- ' bullet")
                )
        if stripped.startswith("- "):
            has_bullet = True

    finish()

    return Notes(sections=tuple(sections), none_line=None, errors=tuple(errors))


def merge_notes(notes_files: Sequence[Notes]) -> Notes:
    """Merge sections across files in file order, then canonical section order.

    A `None. <reason>` file contributes nothing when any file has sections. When
    none does, the last file's line wins (the triggering PR's, last in the walk).
    """
    merged: dict[str, list[str]] = {}
    none_line: str | None = None
    for notes in notes_files:
        if notes.none_line is not None:
            none_line = notes.none_line
            continue
        for section in notes.sections:
            merged.setdefault(section.name, []).extend(section.lines)
    if merged:
        return Notes(
            sections=tuple(
                Section(name=name, lines=tuple(merged[name]))
                for name in _SECTION_ORDER
                if name in merged
            ),
            none_line=None,
            errors=(),
        )
    return Notes(sections=(), none_line=none_line, errors=())


def _read_text(path: str) -> str:
    try:
        if path == "-":
            return sys.stdin.read()
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except UnicodeDecodeError as exc:
        label = "stdin" if path == "-" else path
        raise ChangelogError(f"cannot decode {label} as UTF-8: {exc}") from exc
    except OSError as exc:
        raise ChangelogError(f"cannot read {path}: {exc.strerror or exc}") from exc


def _validate_version(version: str) -> None:
    if not _VERSION_RE.match(version):
        raise ChangelogError(f"invalid version {version!r}; expected X.Y.Z or X.Y.Z-rc.N")


def _validate_date(value: str) -> None:
    if not _DATE_RE.match(value):
        raise ChangelogError(f"invalid date {value!r}; expected YYYY-MM-DD")
    try:
        datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise ChangelogError(f"invalid date {value!r}: {exc}") from exc


def _render_section(notes: Notes, version: str, date: str) -> list[str]:
    block = [f"## [{version}] - {date}", ""]
    if notes.none_line is not None:
        block.append(notes.none_line)
        return block

    ordered = sorted(notes.sections, key=lambda section: _SECTION_ORDER.index(section.name))
    for position, section in enumerate(ordered):
        if position:
            block.append("")
        block.append(f"### {section.name}")
        block.extend(section.lines)
    return block


def render_changelog(
    changelog: str,
    changelog_path: str,
    notes: Notes,
    version: str,
    date: str,
) -> str:
    """Insert the rendered notes under `## [Unreleased]`, refusing stale state."""
    lines = changelog.splitlines()
    visible, _clean, issue = _scan(lines)
    if issue is not None:
        raise ChangelogError(f"{changelog_path}:{issue.line}: {issue.message}")
    unreleased = next(
        (index for index, line in enumerate(visible) if _UNRELEASED_RE.match(line)),
        None,
    )
    if unreleased is None:
        raise ChangelogError(f"{changelog_path} has no '## [Unreleased]' anchor")

    end = next(
        (
            index
            for index in range(unreleased + 1, len(lines))
            if _TOP_HEADING_RE.match(visible[index])
        ),
        len(lines),
    )
    for index in range(unreleased + 1, end):
        if lines[index].strip():
            raise ChangelogError(
                f"{changelog_path}:{index + 1}: content under '## [Unreleased]'; "
                "entries belong in the PR body's '## Changelog' section"
            )

    for index, line in enumerate(visible):
        match = _RELEASE_HEADING_RE.match(line)
        if match is not None and match.group(1) == version:
            raise ChangelogError(
                f"{changelog_path}:{index + 1}: '## [{version}]' already exists; "
                "refusing to render the same release twice"
            )

    rendered = [
        *lines[: unreleased + 1],
        "",
        *_render_section(notes, version, date),
        "",
        *lines[end:],
    ]
    return "\n".join(rendered).rstrip("\n") + "\n"


def _write_atomic(path: str, content: str) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    mode = None
    with contextlib.suppress(OSError):
        mode = os.stat(path).st_mode
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=".changelog-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temporary, stat.S_IMODE(mode))
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _read_notes(path: str) -> Notes:
    return parse_notes(_read_text(path), source=_notes_source(path))


def _read_all_notes(paths: Sequence[str]) -> list[Notes]:
    return [_read_notes(path) for path in paths]


def _report(errors: Sequence[str]) -> int:
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    return 1


def _run_validate(notes_paths: Sequence[str]) -> int:
    notes_files = _read_all_notes(notes_paths)
    errors = tuple(error for notes in notes_files for error in notes.errors)
    if errors:
        return _report(errors)
    notes = merge_notes(notes_files)
    summary = (
        notes.none_line
        if notes.none_line is not None
        else ", ".join(section.name for section in notes.sections)
    )
    print(f"changelog notes valid: {summary}")
    return 0


def _run_render(
    notes_paths: Sequence[str],
    version: str,
    date: str,
    changelog_path: str,
    dry_run: bool,
) -> int:
    _validate_version(version)
    _validate_date(date)
    notes_files = _read_all_notes(notes_paths)
    errors = tuple(error for notes in notes_files for error in notes.errors)
    if errors:
        return _report(errors)
    notes = merge_notes(notes_files)

    rendered = render_changelog(
        _read_text(changelog_path), changelog_path, notes, version, date
    )
    if dry_run:
        sys.stdout.write(rendered)
        return 0
    _write_atomic(changelog_path, rendered)
    print(f"rendered '## [{version}] - {date}' into {changelog_path}")
    return 0


def _run_extract(version: str, changelog_path: str) -> int:
    _validate_version(version)
    lines = _read_text(changelog_path).splitlines()
    visible, _clean, issue = _scan(lines)
    if issue is not None:
        raise ChangelogError(f"{changelog_path}:{issue.line}: {issue.message}")
    start = next(
        (
            index
            for index, line in enumerate(visible)
            if (match := _RELEASE_HEADING_RE.match(line)) is not None
            and match.group(1) == version
        ),
        None,
    )
    if start is None:
        raise ChangelogError(f"{changelog_path} has no '## [{version}]' section")
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if _TOP_HEADING_RE.match(visible[index])
        ),
        len(lines),
    )
    body = list(lines[start + 1 : end])
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    if not body:
        raise ChangelogError(f"{changelog_path}: '## [{version}]' section is empty")
    print("\n".join(body))
    return 0


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser(
        "validate", help="Validate the '## Changelog' section of a PR body."
    )
    validate.add_argument(
        "--notes-file",
        action="append",
        dest="notes_files",
        required=True,
        help="PR body or notes file; repeat for several PRs, or '-' for standard input.",
    )

    render = commands.add_parser(
        "render", help="Render validated notes into CHANGELOG.md under the new version."
    )
    render.add_argument(
        "--notes-file",
        action="append",
        dest="notes_files",
        required=True,
        help="PR body or notes file; repeat for several PRs, or '-' for standard input.",
    )
    render.add_argument("--version", required=True, help="Release version, for example 0.14.11.")
    render.add_argument("--date", required=True, help="Release date, YYYY-MM-DD.")
    render.add_argument(
        "--changelog",
        default=_CHANGELOG_PATH,
        help=f"Changelog file to edit (default: {_CHANGELOG_PATH}).",
    )
    render.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the rendered changelog instead of writing it.",
    )

    extract = commands.add_parser(
        "extract", help="Print a released section for use as the GitHub Release body."
    )
    extract.add_argument("--version", required=True, help="Released version, for example 0.14.10.")
    extract.add_argument(
        "--changelog",
        default=_CHANGELOG_PATH,
        help=f"Changelog file to read (default: {_CHANGELOG_PATH}).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "validate":
            return _run_validate(args.notes_files)
        if args.command == "render":
            return _run_render(
                notes_paths=args.notes_files,
                version=args.version,
                date=args.date,
                changelog_path=args.changelog,
                dry_run=args.dry_run,
            )
        return _run_extract(version=args.version, changelog_path=args.changelog)
    except ChangelogError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
