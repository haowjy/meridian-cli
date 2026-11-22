"""The agent's scoped tool rules as ``agy`` permissions, passed inline with ``--settings``.

Mars decides what ``agy`` can carry: a rule it spells as ``agy`` permission strings
(``command(git reset --hard)``) arrives with them in ``native_permission`` and a ``note`` on
what ``agy`` does not catch. This module groups those strings by
action and unions each list with the person's own, because ``--settings`` replaces a list
instead of merging it. It never writes ``allow``, which would replace the person's allows.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.platform import get_home_path
from meridian.lib.safety.policy import ReportLine, rule_element

# ``agy`` ignores ``ask`` under ``--dangerously-skip-permissions`` (approval ``never``).
_ASK_IGNORED = "agy ignores ask rules under --dangerously-skip-permissions"


def user_settings_path() -> Path:
    """``agy``'s own settings file, beside its conversation store (``state.native_store``)."""

    return get_home_path() / ".gemini" / "antigravity-cli" / "settings.json"


def _user_permissions() -> tuple[dict[str, object], str | None]:
    """The person's ``permissions`` table and, if the file exists but can't be read, why."""

    path = user_settings_path()
    try:
        doc: object = json.loads(path.read_text())
        permissions = cast("dict[str, object]", doc).get("permissions", {})
        if not isinstance(permissions, dict):
            raise ValueError("permissions is not an object")
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError, AttributeError) as exc:
        return {}, f"cannot read {path}: {exc}"
    return cast("dict[str, object]", permissions), None


def compile_agy_permissions(
    spec: ResolvedLaunchSpec,
) -> tuple[str | None, list[ReportLine]]:
    """The inline ``--settings`` value (``None`` when no list has an entry) and report lines.

    The lines name each carried rule, a rule ``approval: never`` leaves out, and a settings
    file of the person's that could not be merged.
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
    if not any(carried.values()):
        return None, lines
    theirs, problem = _user_permissions()
    if problem:
        lines.append(
            ReportLine(
                "agy settings.json",
                "partial",
                f"{problem}; your own deny and ask rules are not in --settings",
            )
        )
    merged: dict[str, list[str]] = {}
    for action, ours in carried.items():
        existing = theirs.get(action, [])
        entries = cast("list[object]", existing) if isinstance(existing, list) else []
        kept = [e for e in entries if isinstance(e, str)]
        if combined := list(dict.fromkeys([*kept, *ours])):
            merged[action] = combined
    return json.dumps({"permissions": merged}, separators=(",", ":")), lines
