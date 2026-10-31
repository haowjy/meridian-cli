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
import json
import os
import re
import stat
import sys
import tempfile
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

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
_BACKTICKS_RE = re.compile(r"`+")
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
_UNRELEASED_CONTENT_MESSAGE = (
    "content under '## [Unreleased]'; entries belong in the PR body's '## Changelog' section"
)


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


def _mask_inline(line: str) -> str:
    """Replace inline code spans with inert same-length text.

    A span is opened and closed by backtick runs of equal length, as in
    CommonMark, so a longer run such as ``` in prose does not swallow the
    `` `<!--` `` after it.
    """
    masked = list(line)
    runs = [(match.start(), len(match.group(0))) for match in _BACKTICKS_RE.finditer(line)]
    index = 0
    while index < len(runs):
        start, length = runs[index]
        close = next(
            (later for later in range(index + 1, len(runs)) if runs[later][1] == length),
            None,
        )
        if close is None:
            index += 1
            continue
        for position in range(start, runs[close][0] + length):
            masked[position] = "x"
        index = close + 1
    return "".join(masked)


def _scan(lines: Sequence[str]) -> tuple[list[str], list[str], tuple[int, str] | None]:
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
            end = _mask_inline(line).find("-->")
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

        # Inline code is masked before the comment scan, so a `<!--` inside a
        # code span is an example rather than an opener; `clean` keeps the code.
        masked = _mask_inline(prose)
        while (start := masked.find("<!--")) != -1:
            end = masked.find("-->", start + 4)
            if end == -1:
                prose = prose[:start]
                masked = masked[:start]
                in_comment = True
                comment_line = number
                break
            prose = prose[:start] + prose[end + 3 :]
            masked = masked[:start] + masked[end + 3 :]

        visible.append(masked.rstrip())
        clean.append(prose.rstrip())

    issue: tuple[int, str] | None = None
    if fence is not None:
        issue = (fence_line, "unclosed fenced code block; add a closing fence")
    elif in_comment:
        issue = (comment_line, "unclosed HTML comment; add '-->'")
    return visible, clean, issue


def _error(source: str, line: int | None, message: str) -> str:
    if line is None:
        return f"{source}: {message}"
    return f"{source}:{line}: {message}"


def _block_end(visible: Sequence[str], start: int) -> int:
    """Index of the next '#' or '##' heading after `start`, or the line count."""
    return next(
        (
            index
            for index in range(start + 1, len(visible))
            if _TOP_HEADING_RE.match(visible[index])
        ),
        len(visible),
    )


def parse_notes(text: str, source: str = "PR body") -> tuple[Notes, tuple[str, ...]]:
    """Parse the `## Changelog` section of a notes file, collecting every error."""
    lines = text.splitlines()
    visible, clean, issue = _scan(lines)
    errors: list[str] = []
    if issue is not None:
        line, message = issue
        errors.append(_error(source, line, message))
        return Notes(sections=(), none_line=None), tuple(errors)

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
        return Notes(sections=(), none_line=None), tuple(errors)
    if len(heading_lines) > 1:
        for duplicate in heading_lines[1:]:
            errors.append(
                _error(source, duplicate + 1, "second '## Changelog' section; keep exactly one")
            )

    start = heading_lines[0]
    end = _block_end(visible, start)
    content = range(start + 1, end)

    # A section-name heading anywhere but the section itself is a block that
    # fell out: the author wrote it after another `##` heading, or a level-1/2
    # heading ended the section early. No PR-template knowledge is needed.
    for index, line in enumerate(visible):
        if start < index < end:
            continue
        heading = _ANY_HEADING_RE.match(line.strip())
        if heading is not None and heading.group(2).strip() in _SECTION_ORDER:
            errors.append(
                _error(
                    source,
                    index + 1,
                    f"{line.strip()!r} is outside '## Changelog'; put the changelog "
                    "blocks under the '## Changelog' heading (a '#' or '##' heading "
                    "ends the section)",
                )
            )
            break

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
        return Notes(sections=(), none_line=None), tuple(errors)

    first_text = visible[first].strip()
    none_line_only = first_text.startswith("None.")
    for index in content:
        text = visible[index].strip()
        if not text or (none_line_only and index == first):
            continue
        candidate = text[2:].strip() if text.startswith("- ") else text
        if _PLACEHOLDER_ONLY_RE.fullmatch(candidate):
            errors.append(
                _error(source, index + 1, f"placeholder {candidate!r}; write what changed")
            )
            continue
        match = _PLACEHOLDER_RE.search(text)
        if match is not None:
            errors.append(_error(source, index + 1, f"leftover placeholder {match.group(0)!r}"))

    if none_line_only:
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
        return Notes(sections=(), none_line=none_line), tuple(errors)

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
                    f"expected 'None. <reason>' or '### <Section>', got: {clean[index].strip()!r}",
                )
            )
            continue

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

    return Notes(sections=tuple(sections), none_line=None), tuple(errors)


def merge_notes(notes_files: Sequence[Notes]) -> Notes:
    """Merge sections across files in file order, then canonical section order.

    A `None. <reason>` file contributes nothing when any file has sections. When
    none does, the last file's line wins (the walk appends the trigger last).
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
        )
    return Notes(sections=(), none_line=none_line)


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
    """Render notes whose sections `merge_notes` already put in canonical order."""
    block = [f"## [{version}] - {date}", ""]
    if notes.none_line is not None:
        block.append(notes.none_line)
        return block

    for position, section in enumerate(notes.sections):
        if position:
            block.append("")
        block.append(f"### {section.name}")
        block.extend(section.lines)
    return block


def _visible_lines(text: str, path: str) -> tuple[list[str], list[str]]:
    """Split `text` into its raw and scan views, refusing an unterminated block."""
    lines = text.splitlines()
    visible, _clean, issue = _scan(lines)
    if issue is not None:
        line, message = issue
        raise ChangelogError(f"{path}:{line}: {message}")
    return lines, visible


def _release_headings(visible: Sequence[str]) -> list[tuple[int, str]]:
    """Every `## [X]` heading's line number and label, in file order."""
    return [
        (index + 1, match.group(1))
        for index, line in enumerate(visible)
        if (match := _RELEASE_HEADING_RE.match(line)) is not None
    ]


def _unreleased_block(
    lines: Sequence[str], visible: Sequence[str], path: str
) -> tuple[int, list[int]]:
    """Index of the `## [Unreleased]` anchor plus the lines that carry content."""
    unreleased = next(
        (index for index, line in enumerate(visible) if _UNRELEASED_RE.match(line)),
        None,
    )
    if unreleased is None:
        raise ChangelogError(f"{path} has no '## [Unreleased]' anchor")
    end = _block_end(visible, unreleased)
    return unreleased, [index for index in range(unreleased + 1, end) if lines[index].strip()]


def render_changelog(
    changelog: str,
    changelog_path: str,
    notes: Notes,
    version: str,
    date: str,
) -> str:
    """Insert the rendered notes under `## [Unreleased]`, refusing stale state."""
    lines, visible = _visible_lines(changelog, changelog_path)
    unreleased, content = _unreleased_block(lines, visible, changelog_path)
    if content:
        raise ChangelogError(_error(changelog_path, content[0] + 1, _UNRELEASED_CONTENT_MESSAGE))
    end = _block_end(visible, unreleased)

    for index, line in enumerate(visible):
        match = _RELEASE_HEADING_RE.match(line)
        if match is not None and match.group(1) == version:
            raise ChangelogError(
                f"{changelog_path}:{index + 1}: '## [{version}]' already exists; "
                "refusing to render the same release twice. If the release commit "
                "exists without its tag, run scripts/manually-release.sh tag-current."
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


def _read_notes(path: str) -> tuple[Notes, tuple[str, ...]]:
    return parse_notes(_read_text(path), source="stdin" if path == "-" else path)


def _read_notes_json(path: str) -> list[tuple[Notes, tuple[str, ...]]]:
    """Read the collector's `[{"number": N, "body": "..."}]` into parsed notes."""
    try:
        raw = cast("object", json.loads(_read_text(path)))
    except json.JSONDecodeError as exc:
        raise ChangelogError(f"{path}: invalid notes JSON: {exc}") from exc
    if not isinstance(raw, list):
        raise ChangelogError(
            f'{path}: expected a JSON array of {{"number": N, "body": "..."}} entries'
        )
    entries = cast("list[object]", raw)
    parsed: list[tuple[Notes, tuple[str, ...]]] = []
    for position, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ChangelogError(
                f"{path}: entry {position} needs an integer 'number' and a string 'body'"
            )
        fields = cast("dict[str, object]", entry)
        number = fields.get("number")
        body = fields.get("body")
        if not isinstance(number, int) or not isinstance(body, str):
            raise ChangelogError(
                f"{path}: entry {position} needs an integer 'number' and a string 'body'"
            )
        parsed.append(parse_notes(body, source=f"PR #{number}"))
    return parsed


def _read_all_notes(
    notes_json_paths: Sequence[str], notes_paths: Sequence[str]
) -> list[tuple[Notes, tuple[str, ...]]]:
    """Read collected bodies first, then extra files, both in argument order."""
    parsed: list[tuple[Notes, tuple[str, ...]]] = []
    for path in notes_json_paths:
        parsed.extend(_read_notes_json(path))
    for path in notes_paths:
        parsed.append(_read_notes(path))
    return parsed


def _all_errors(parsed: Sequence[tuple[Notes, tuple[str, ...]]]) -> tuple[str, ...]:
    return tuple(error for _notes, errors in parsed for error in errors)


def _report(errors: Sequence[str]) -> int:
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    return 1


def _changelog_errors(changelog_path: str, base_changelog_path: str | None) -> list[str]:
    """Check a PR's changelog: no new `[Unreleased]` content, no new release heading.

    With a base file, only this branch's own edits count. Content and headings
    the base already carries are main's, and failing every open branch for them
    would block all PRs after a release or an old-style entry.
    """
    head_lines, head_visible = _visible_lines(_read_text(changelog_path), changelog_path)
    head_content = _unreleased_block(head_lines, head_visible, changelog_path)[1]
    errors: list[str] = []
    if base_changelog_path is None:
        if head_content:
            errors.append(_error(changelog_path, head_content[0] + 1, _UNRELEASED_CONTENT_MESSAGE))
        return errors

    base_text = _read_text(base_changelog_path)
    base_lines, base_visible = _visible_lines(base_text, base_changelog_path)
    base_content = _unreleased_block(base_lines, base_visible, base_changelog_path)[1]
    if head_content and not base_content:
        errors.append(_error(changelog_path, head_content[0] + 1, _UNRELEASED_CONTENT_MESSAGE))

    base_counts = Counter(label for _line, label in _release_headings(base_visible))
    seen: Counter[str] = Counter()
    for line, label in _release_headings(head_visible):
        seen[label] += 1
        if seen[label] > base_counts[label]:
            errors.append(
                _error(
                    changelog_path,
                    line,
                    f"this PR adds '## [{label}]'; changelog entries belong in the "
                    "PR body's '## Changelog' section",
                )
            )
    return errors


def _run_validate(
    notes_paths: Sequence[str],
    changelog_path: str | None,
    base_changelog_path: str | None,
) -> int:
    if base_changelog_path is not None and changelog_path is None:
        raise ChangelogError("--base-changelog needs --changelog (the file to compare it with)")
    parsed = _read_all_notes((), notes_paths)
    errors = list(_all_errors(parsed))
    if changelog_path is not None:
        errors.extend(_changelog_errors(changelog_path, base_changelog_path))
    if errors:
        return _report(errors)
    notes = merge_notes([notes for notes, _errors in parsed])
    summary = (
        notes.none_line
        if notes.none_line is not None
        else ", ".join(section.name for section in notes.sections)
    )
    print(f"changelog notes valid: {summary}")
    if changelog_path is not None:
        print(f"changelog file valid: {changelog_path}")
    return 0


def _run_render(
    notes_json_paths: Sequence[str],
    notes_paths: Sequence[str],
    version: str,
    date: str,
    changelog_path: str,
    dry_run: bool,
) -> int:
    _validate_version(version)
    _validate_date(date)
    if not notes_json_paths and not notes_paths:
        raise ChangelogError("render needs at least one --notes-json or --notes-file")
    parsed = _read_all_notes(notes_json_paths, notes_paths)
    errors = _all_errors(parsed)
    if errors:
        return _report(errors)
    notes = merge_notes([notes for notes, _errors in parsed])

    rendered = render_changelog(_read_text(changelog_path), changelog_path, notes, version, date)
    if dry_run:
        sys.stdout.write(rendered)
        return 0
    _write_atomic(changelog_path, rendered)
    print(f"rendered '## [{version}] - {date}' into {changelog_path}")
    return 0


def _run_extract(version: str, changelog_path: str) -> int:
    _validate_version(version)
    lines, visible = _visible_lines(_read_text(changelog_path), changelog_path)
    start = next(
        (
            index
            for index, line in enumerate(visible)
            if (match := _RELEASE_HEADING_RE.match(line)) is not None and match.group(1) == version
        ),
        None,
    )
    if start is None:
        raise ChangelogError(f"{changelog_path} has no '## [{version}]' section")
    end = _block_end(visible, start)
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
    validate.add_argument(
        "--changelog",
        help="Also refuse content under '## [Unreleased]' in this changelog file.",
    )
    validate.add_argument(
        "--base-changelog",
        help="With --changelog, also refuse a release heading this branch adds "
        "(compared with the base branch's copy of the file).",
    )

    render = commands.add_parser(
        "render", help="Render validated notes into CHANGELOG.md under the new version."
    )
    render.add_argument(
        "--notes-json",
        action="append",
        dest="notes_json",
        default=[],
        help="JSON array of {number, body} from collect-release-notes.sh; repeatable.",
    )
    render.add_argument(
        "--notes-file",
        action="append",
        dest="notes_files",
        default=[],
        help="Extra notes file or PR body rendered after the collected ones; "
        "repeat for several, or '-' for standard input.",
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
            return _run_validate(args.notes_files, args.changelog, args.base_changelog)
        if args.command == "render":
            return _run_render(
                notes_json_paths=args.notes_json,
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
