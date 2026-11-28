"""The agent's scoped tool rules as ``agy`` permissions, passed inline with ``--settings``.

Mars decides what ``agy`` can carry: a rule it spells as ``agy`` permission strings
(``command(git reset --hard)``) arrives with them in ``native_permission`` and a ``note`` on
what ``agy`` does not catch. This module groups those strings by
action and adds each list to the one ``agy`` would have used without Meridian: the list in a
``--settings`` the person passed, else the one in their ``settings.json``. Both are needed
because ``--settings`` replaces a list key by key and ``agy`` uses the last ``--settings`` only.
Every list Meridian doesn't add to (``allow``, ``deny`` when no rule is carried) is left as
``agy`` would read it, so the person's own settings behave as they would without Meridian.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from meridian.lib.core.permission_policy import ReportLine, rule_element
from meridian.lib.harness.agy.state import user_settings_path
from meridian.lib.launch.launch_types import ResolvedLaunchSpec

# ``agy`` ignores ``ask`` under ``--dangerously-skip-permissions`` (approval ``never``).
_ASK_IGNORED = "agy ignores ask rules under --dangerously-skip-permissions"


@dataclass(frozen=True)
class AgyPermissions:
    """What a launch passes to ``agy``: one ``--settings`` value and the arguments around it."""

    settings: str | None
    extra_args: tuple[str, ...]
    lines: tuple[ReportLine, ...]


# agy parses flags Go-style, so one dash works as well as two.
_SETTINGS_FLAGS = ("--settings", "-settings")
_SETTINGS_PREFIXES = ("--settings=", "-settings=")


def _split_settings_arg(args: tuple[str, ...]) -> tuple[str | None, tuple[str, ...]]:
    """The last ``--settings`` value in ``args`` (``agy`` uses the last) and the rest."""

    value: str | None = None
    rest: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in _SETTINGS_FLAGS and i + 1 < len(args):
            value = args[i + 1]
            i += 2
            continue
        if arg.startswith(_SETTINGS_PREFIXES):
            value = arg.partition("=")[2]
            i += 1
            continue
        rest.append(arg)
        i += 1
    return value, tuple(rest)


def _read_settings(source: str, *, inline: bool, cwd: Path) -> dict[str, object]:
    """A settings document: inline JSON (``--settings`` value) or the file at ``source``."""

    text = source if inline else (cwd / Path(source).expanduser()).read_text()
    doc: object = json.loads(text)
    if not isinstance(doc, dict):
        raise ValueError("not a JSON object")
    permissions = cast("dict[str, object]", doc).get("permissions", {})
    if not isinstance(permissions, dict):
        raise ValueError("permissions is not an object")
    return cast("dict[str, object]", doc)


def _permissions_of(doc: dict[str, object]) -> dict[str, object]:
    return cast("dict[str, object]", doc.get("permissions", {}))


def _strings(permissions: dict[str, object], action: str) -> list[str]:
    entries = permissions.get(action, [])
    return (
        [e for e in cast("list[object]", entries) if isinstance(e, str)]
        if isinstance(entries, list)
        else []
    )


def compile_agy_permissions(spec: ResolvedLaunchSpec, cwd: Path) -> AgyPermissions:
    """The one ``--settings`` value (``None`` when nothing needs one), the other extra args and
    report lines.

    The lines name each carried rule, a rule ``approval: never`` leaves out, and a settings
    source of the person's that could not be merged (their ``--settings`` stays as they gave
    it, and ``settings.json`` is left out of ours).
    """

    policy = spec.permission_policy
    never = policy.approval == "never"
    carried: dict[str, list[str]] = {"deny": [], "ask": []}
    lines: list[ReportLine] = []
    for rule in policy.rules:
        permissions = list(rule.native_permission)
        if rule.covered_by is not None or rule.action == "allow" or not permissions:
            continue
        if rule.action == "ask" and never:
            lines.append(ReportLine(rule_element(rule), "unsupported", _ASK_IGNORED))
            continue
        carried[rule.action].extend(permissions)
        lines.append(
            ReportLine(
                rule_element(rule),
                "native",
                f"--settings permissions.{rule.action}: {', '.join(permissions)}",
            )
        )
    given, extra_args = _split_settings_arg(spec.extra_args)
    if not any(carried.values()):
        return AgyPermissions(given, extra_args, tuple(lines))
    doc: dict[str, object] = {}
    sources: list[dict[str, object]] = []
    problems: list[str] = []
    stored = user_settings_path()
    try:
        sources.append(_read_settings(str(stored), inline=False, cwd=cwd))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        problems.append(f"cannot read {stored}: {exc}")
    if given is not None:
        try:
            doc = _read_settings(given, inline=given.lstrip().startswith("{"), cwd=cwd)
            sources.append(doc)
        except (OSError, ValueError) as exc:
            lines.append(
                ReportLine(
                    "agy --settings",
                    "partial",
                    f"cannot read {given}: {exc}; passed as given, without the agent's rules",
                )
            )
            return AgyPermissions(given, extra_args, tuple(lines))
    for problem in problems:
        lines.append(
            ReportLine(
                "agy settings.json",
                "partial",
                f"{problem}; your own deny and ask rules are not in --settings",
            )
        )
    permissions = dict(cast("dict[str, object]", doc.get("permissions", {})))
    for action, ours in carried.items():
        if not ours:
            continue
        # The source agy reads this list from: a later ``--settings`` replaces the key.
        empty: dict[str, object] = {}
        source = next((s for s in reversed(sources) if action in _permissions_of(s)), empty)
        theirs = _strings(_permissions_of(source), action)
        permissions[action] = list(dict.fromkeys([*theirs, *ours]))
    merged = {**doc, "permissions": permissions}
    return AgyPermissions(json.dumps(merged, separators=(",", ":")), extra_args, tuple(lines))
