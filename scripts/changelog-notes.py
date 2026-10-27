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
_CHANGELOG_PATH = "CHANGELOG.md"
_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CHANGELOG_HEADING_RE = re.compile(r"^##\s+Changelog\s*$")
_ANY_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_TOP_HEADING_RE = re.compile(r"^#{1,2}\s+")
_RELEASE_HEADING_RE = re.compile(r"^##\s+\[([^\]]+)\]")
_UNRELEASED_RE = re.compile(r"^##\s+\[Unreleased\]\s*$")
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})")
# Angle brackets with whitespace inside are template prose, never a command
# metavariable such as `<id>` or `<path>`.
_PLACEHOLDER_RE = re.compile(r"<(?:[^<>\n]*\s[^<>\n]*|fill in|placeholder|\.\.\.)>")
_NONE_RE = re.compile(r"^None\.\s+\S")


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


def _scan(lines: Sequence[str]) -> tuple[list[str], list[str]]:
    """Return (visible, clean) line views of equal length.

    `visible` blanks fenced code blocks and strips HTML comments, so structure
    and placeholders are read from prose only. `clean` keeps fenced code (a
    changelog entry may carry an example) and strips comments, so rendering
    never copies guidance comments into CHANGELOG.md.
    """
    visible: list[str] = []
    clean: list[str] = []
    fence: str | None = None
    in_comment = False

    for line in lines:
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

        fence_match = _FENCE_RE.match(stripped)
        if fence_match is not None:
            fence = fence_match.group(1)
            visible.append("")
            clean.append(line.rstrip())
            continue

        prose = line
        if in_comment:
            end = prose.find("-->")
            if end == -1:
                visible.append("")
                clean.append("")
                continue
            prose = prose[end + 3 :]
            in_comment = False

        while True:
            start = prose.find("<!--")
            if start == -1:
                break
            end = prose.find("-->", start + 4)
            if end == -1:
                prose = prose[:start]
                in_comment = True
                break
            prose = prose[:start] + prose[end + 3 :]

        prose = prose.rstrip()
        visible.append(prose)
        clean.append(prose)

    return visible, clean


def _error(line_number: int | None, message: str) -> str:
    if line_number is None:
        return f"PR body: {message}"
    return f"line {line_number}: {message}"


def parse_notes(text: str) -> Notes:
    """Parse the `## Changelog` section of a PR body, collecting every error."""
    lines = text.splitlines()
    visible, clean = _scan(lines)
    errors: list[str] = []

    heading_lines = [
        index for index, line in enumerate(visible) if _CHANGELOG_HEADING_RE.match(line)
    ]
    if not heading_lines:
        errors.append(
            _error(None, "no '## Changelog' section; add one or write 'None. <reason>' under it")
        )
        return Notes(sections=(), none_line=None, errors=tuple(errors))
    if len(heading_lines) > 1:
        for duplicate in heading_lines[1:]:
            errors.append(
                _error(duplicate + 1, "second '## Changelog' section; keep exactly one")
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

    for index in content:
        match = _PLACEHOLDER_RE.search(visible[index])
        if match is not None:
            errors.append(
                _error(index + 1, f"leftover placeholder {match.group(0)!r}")
            )

    first = next((index for index in content if visible[index].strip()), None)
    if first is None:
        errors.append(
            _error(
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
                    first + 1,
                    "'None.' needs a reason, for example 'None. Internal refactor only.'",
                )
            )
        if _PLACEHOLDER_RE.fullmatch(none_line[len("None.") :].strip()) or re.fullmatch(
            r"<[^<>]+>", none_line[len("None.") :].strip()
        ):
            errors.append(_error(first + 1, "leftover placeholder in the 'None.' reason"))
        for index in content:
            if index > first and visible[index].strip():
                errors.append(
                    _error(
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

    def finish() -> None:
        nonlocal name, name_line, body, started, has_bullet
        if name is None:
            return
        while body and not body[0].strip():
            body.pop(0)
        while body and not body[-1].strip():
            body.pop()
        if not has_bullet:
            errors.append(_error(name_line, f"'### {name}' has no '- ' bullet"))
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
                        index + 1,
                        f"{stripped!r} is not a changelog section; use '### <Section>' "
                        f"with one of: {_SECTION_LIST}",
                    )
                )
                continue
            unknown_section = False
            if title in seen:
                errors.append(_error(index + 1, f"duplicate '### {title}' section"))
            seen.add(title)
            name = title
            name_line = index + 1
            continue

        if unknown_section:
            continue

        if name is None:
            errors.append(
                _error(
                    index + 1,
                    f"expected 'None. <reason>' or '### <Section>', got: {clean[index].strip()!r}",
                )
            )
            continue

        body.append(clean[index])
        if not started:
            started = True
            if not stripped.startswith("- "):
                errors.append(
                    _error(index + 1, f"'### {name}' must start with a '- ' bullet")
                )
        if stripped.startswith("- "):
            has_bullet = True

    finish()

    return Notes(sections=tuple(sections), none_line=None, errors=tuple(errors))


def _read_text(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
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


def render_changelog(changelog: str, notes: Notes, version: str, date: str) -> str:
    """Insert the rendered notes under `## [Unreleased]`, refusing stale state."""
    lines = changelog.splitlines()
    visible, _clean = _scan(lines)
    unreleased = next(
        (index for index, line in enumerate(visible) if _UNRELEASED_RE.match(line)),
        None,
    )
    if unreleased is None:
        raise ChangelogError(f"{_CHANGELOG_PATH} has no '## [Unreleased]' anchor")

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
                f"{_CHANGELOG_PATH}:{index + 1}: content under '## [Unreleased]'; "
                "entries belong in the PR body's '## Changelog' section"
            )

    for index, line in enumerate(visible):
        match = _RELEASE_HEADING_RE.match(line)
        if match is not None and match.group(1) == version:
            raise ChangelogError(
                f"{_CHANGELOG_PATH}:{index + 1}: '## [{version}]' already exists; "
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
    return parse_notes(_read_text(path))


def _report(errors: Sequence[str]) -> int:
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    return 1


def _run_validate(notes_path: str) -> int:
    notes = _read_notes(notes_path)
    if not notes.valid:
        return _report(notes.errors)
    summary = notes.none_line if notes.none_line is not None else ", ".join(
        section.name for section in notes.sections
    )
    print(f"changelog notes valid: {summary}")
    return 0


def _run_render(
    notes_path: str,
    version: str,
    date: str,
    changelog_path: str,
    dry_run: bool,
) -> int:
    _validate_version(version)
    _validate_date(date)
    notes = _read_notes(notes_path)
    if not notes.valid:
        return _report(notes.errors)

    rendered = render_changelog(_read_text(changelog_path), notes, version, date)
    if dry_run:
        sys.stdout.write(rendered)
        return 0
    _write_atomic(changelog_path, rendered)
    print(f"rendered '## [{version}] - {date}' into {changelog_path}")
    return 0


def _run_extract(version: str, changelog_path: str) -> int:
    _validate_version(version)
    lines = _read_text(changelog_path).splitlines()
    visible, _clean = _scan(lines)
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
        required=True,
        help="PR body file, or '-' to read standard input.",
    )

    render = commands.add_parser(
        "render", help="Render validated notes into CHANGELOG.md under the new version."
    )
    render.add_argument(
        "--notes-file",
        required=True,
        help="PR body file, or '-' to read standard input.",
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
            return _run_validate(args.notes_file)
        if args.command == "render":
            return _run_render(
                notes_path=args.notes_file,
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
