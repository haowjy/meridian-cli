#!/usr/bin/env python3
"""Reject CHANGELOG additions that landed under an already released version."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass

_CHANGELOG_PATH = "CHANGELOG.md"
_HEADING_RE = re.compile(r"^## \[([^]]+)](?:\s.*)?$")
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_FIX_HINT = (
    "move it under ## [Unreleased]; after a rebase across a release commit, rebuild "
    "CHANGELOG.md from `git show origin/main:CHANGELOG.md` plus your entry"
)


class CheckError(RuntimeError):
    """Raised when the changelog placement check cannot run reliably."""


@dataclass(frozen=True)
class Heading:
    text: str
    version: str


@dataclass(frozen=True)
class Finding:
    line_number: int
    text: str
    section: str


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _resolve_commit(ref: str) -> str | None:
    result = _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _require_git_output(*args: str) -> str:
    result = _git(*args)
    if result.returncode == 0:
        return result.stdout
    detail = result.stderr.strip() or f"git {' '.join(args)} failed"
    raise CheckError(detail)


def _added_line_numbers(diff: str) -> tuple[int, ...]:
    added: list[int] = []
    new_line_number: int | None = None

    for line in diff.splitlines():
        hunk = _HUNK_RE.match(line)
        if hunk is not None:
            new_line_number = int(hunk.group(1))
            continue
        if new_line_number is None:
            continue
        if line.startswith("+"):
            if line[1:].strip():
                added.append(new_line_number)
            new_line_number += 1
        elif line.startswith("-") or line.startswith("\\"):
            continue
        elif line.startswith(" "):
            new_line_number += 1
        else:
            new_line_number = None

    return tuple(added)


def _headings_by_line(contents: str) -> tuple[list[str], dict[int, Heading]]:
    lines = contents.splitlines()
    headings: dict[int, Heading] = {}
    current: Heading | None = None

    for line_number, line in enumerate(lines, start=1):
        match = _HEADING_RE.match(line)
        if match is not None:
            current = Heading(text=line, version=match.group(1))
        if current is not None:
            headings[line_number] = current

    return lines, headings


def _released_versions(contents: str) -> set[str]:
    versions: set[str] = set()
    for line in contents.splitlines():
        match = _HEADING_RE.match(line)
        if match is not None and match.group(1) != "Unreleased":
            versions.add(match.group(1))
    return versions


def _find_misfiled_lines(
    *,
    added_line_numbers: Sequence[int],
    head_contents: str,
    base_contents: str,
) -> tuple[Finding, ...]:
    lines, headings = _headings_by_line(head_contents)
    base_versions = _released_versions(base_contents)
    findings: list[Finding] = []

    for line_number in added_line_numbers:
        if line_number > len(lines):
            raise CheckError(
                f"diff line {line_number} is outside HEAD's {_CHANGELOG_PATH}"
            )
        heading = headings.get(line_number)
        if heading is None or heading.version not in base_versions:
            continue
        findings.append(
            Finding(
                line_number=line_number,
                text=lines[line_number - 1],
                section=heading.text,
            )
        )

    return tuple(findings)


def _annotation_message(finding: Finding) -> str:
    message = f"added line is under released section {finding.section}: {finding.text}"
    return message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _report_findings(findings: Sequence[Finding], *, warning: bool) -> None:
    level = "warning" if warning else "error"
    github_actions = os.environ.get("GITHUB_ACTIONS") == "true"

    for finding in findings:
        print(
            f"{level}: {_CHANGELOG_PATH}:{finding.line_number}: "
            f"added line under {finding.section}: {finding.text}",
            file=sys.stderr,
        )
        if github_actions:
            print(
                f"::{level} file={_CHANGELOG_PATH},line={finding.line_number}::"
                f"{_annotation_message(finding)}",
                file=sys.stderr,
            )
    print(f"{level}: fix: {_FIX_HINT}", file=sys.stderr)


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        default="origin/main",
        help="Base ref whose released version headings are authoritative.",
    )
    parser.add_argument(
        "--head",
        default="HEAD",
        help="Head ref whose CHANGELOG additions are checked.",
    )
    parser.add_argument(
        "--allow-released",
        action="store_true",
        help="Warn instead of failing for intentional edits to released sections.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    base_sha = _resolve_commit(args.base)
    if base_sha is None:
        print(f"skipped: base ref {args.base!r} is unavailable")
        return 0

    head_sha = _resolve_commit(args.head)
    if head_sha is None:
        print(f"error: head ref {args.head!r} is unavailable", file=sys.stderr)
        return 1

    try:
        merge_base = _require_git_output("merge-base", base_sha, head_sha).strip()
        diff = _require_git_output(
            "diff",
            "--unified=0",
            "--no-color",
            "--no-ext-diff",
            merge_base,
            head_sha,
            "--",
            _CHANGELOG_PATH,
        )
        if not diff:
            print(f"{_CHANGELOG_PATH} unchanged; nothing to check")
            return 0

        added_line_numbers = _added_line_numbers(diff)
        if not added_line_numbers:
            print(f"{_CHANGELOG_PATH} has no added non-blank lines; nothing to check")
            return 0

        head_contents = _require_git_output("show", f"{head_sha}:{_CHANGELOG_PATH}")
        base_contents = _require_git_output("show", f"{base_sha}:{_CHANGELOG_PATH}")
        findings = _find_misfiled_lines(
            added_line_numbers=added_line_numbers,
            head_contents=head_contents,
            base_contents=base_contents,
        )
    except CheckError as exc:
        print(f"error: changelog placement check failed: {exc}", file=sys.stderr)
        return 1

    if not findings:
        print("CHANGELOG placement check passed")
        return 0

    _report_findings(findings, warning=args.allow_released)
    return 0 if args.allow_released else 1


if __name__ == "__main__":
    raise SystemExit(main())
