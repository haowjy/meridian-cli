"""Compile Meridian's permission policy to ordered OpenCode agent rules.

OpenCode resolves the last matching rule. Meridian's policy is order-independent and
resolves the most specific match, so the compile orders the global default first,
unscoped tool rules next, and scoped rules from least to most literal specificity.
Equal matches are ordered ``allow``, ``ask``, ``deny``. Platform denies follow the
profile, and the spawn-completion exceptions follow those denies.

The compile also canonicalizes V2 action aliases before resolving collisions. Because
V2 merges ``write`` and ``patch`` into ``edit``, conflicting rules for those semantic
capabilities collapse to the strictest broad rule rather than widening either one. The
config projector only renders this typed result as the V1 nested ``permission`` map or
the V2 ``permissions`` rule list; nothing else decides OpenCode permissions.
"""

from __future__ import annotations

from dataclasses import dataclass

from meridian.lib.harness.opencode_backend import OpenCodeVersion, bound_opencode_version
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.policy import (
    EnforcementReport,
    ReportLine,
    complete_report,
    platform_element,
    rule_element,
)
from meridian.lib.tools import ToolAction, ToolRule, split_native, unscoped_action

_STRICTNESS: dict[ToolAction, int] = {"allow": 0, "ask": 1, "deny": 2}

# Platform-rule capabilities are semantic; these are the OpenCode V1 actions that
# implement them. The compile aliases them when producing V2 rules.
_PLATFORM_DENY_ACTIONS: dict[str, tuple[str, ...]] = {
    "agent": ("task",),
    "task": ("todowrite",),
}

_V2_ACTION_ALIASES: dict[str, str] = {
    "bash": "shell",
    "write": "edit",
    "patch": "edit",
    "task": "subagent",
}


@dataclass(frozen=True)
class OpenCodePermissionRule:
    """One ordered rule in the selected OpenCode version's action vocabulary."""

    action: str
    resource: str
    effect: ToolAction


type OpenCodePermissions = tuple[OpenCodePermissionRule, ...]


def _effective_action(action: ToolAction, *, interactive: bool) -> ToolAction:
    return "deny" if action == "ask" and not interactive else action


def _native_action(action: str, *, version: OpenCodeVersion) -> str:
    return _V2_ACTION_ALIASES.get(action, action) if version == "v2" else action


def _rule_entries(
    rule: ToolRule, *, interactive: bool, version: OpenCodeVersion
) -> tuple[OpenCodePermissionRule, ...]:
    effect = _effective_action(rule.action, interactive=interactive)
    if rule.is_wildcard:
        return (OpenCodePermissionRule(action="*", resource="*", effect=effect),)
    if rule.native_status != "known":
        return ()
    return tuple(
        OpenCodePermissionRule(
            action=_native_action(action, version=version),
            resource=resource if resource is not None else "*",
            effect=effect,
        )
        for action, resource in map(split_native, rule.native)
    )


def _specificity(rule: OpenCodePermissionRule) -> tuple[int, int, int, int]:
    """Order OpenCode's last-match rules to implement Meridian specificity."""

    tier = 0 if rule.action == "*" else 1 if rule.resource == "*" else 2
    return (
        tier,
        len(rule.action.replace("*", "")),
        len(rule.resource.replace("*", "")),
        _STRICTNESS[rule.effect],
    )


def _collapse_exact_rules(
    rules: list[OpenCodePermissionRule],
) -> tuple[OpenCodePermissionRule, ...]:
    """Make identical native matches deterministic: the stricter effect wins."""

    strongest: dict[tuple[str, str], OpenCodePermissionRule] = {}
    for rule in rules:
        key = (rule.action, rule.resource)
        current = strongest.get(key)
        if current is None or _STRICTNESS[rule.effect] > _STRICTNESS[current.effect]:
            strongest[key] = rule
    return tuple(sorted(strongest.values(), key=_specificity))


def _v2_edit_conflict(
    entries_by_rule: list[tuple[ToolRule, tuple[OpenCodePermissionRule, ...]]],
    *,
    interactive: bool,
) -> ToolAction | None:
    """Return the conservative effect needed for incompatible V2 edit aliases."""

    effects_by_source: dict[str, set[ToolAction]] = {}
    for rule, entries in entries_by_rule:
        if not any(entry.action == "edit" for entry in entries):
            continue
        effect = _effective_action(rule.action, interactive=interactive)
        native_sources = {
            split_native(native)[0]
            for native in rule.native
            if _native_action(split_native(native)[0], version="v2") == "edit"
        }
        source = (
            rule.capability
            if rule.capability in {"write", "patch", "edit"}
            else next(iter(native_sources), "edit")
        )
        effects_by_source.setdefault(source, set()).add(effect)

    effects: set[ToolAction] = set()
    for source_effects in effects_by_source.values():
        effects.update(source_effects)
    if len(effects_by_source) < 2 or len(effects) < 2:
        return None
    return max(effects, key=_STRICTNESS.__getitem__)


def _uses_v2_write_patch_alias(rule: ToolRule) -> bool:
    return rule.capability in {"write", "patch"} or any(
        split_native(native)[0] in {"write", "patch"} for native in rule.native
    )


def _default_entry(
    spec: ResolvedLaunchSpec, *, has_profile_native: bool
) -> OpenCodePermissionRule | None:
    """Return the approval/fallback rule when no explicit ``*`` rule decides it."""

    policy = spec.permission_policy
    if unscoped_action(policy.rules, "*") is not None:
        return None
    if policy.approval == "confirm":
        return OpenCodePermissionRule(
            action="*",
            resource="*",
            effect="ask" if spec.interactive else "deny",
        )
    if policy.approval in {"auto", "never", "yolo"}:
        return OpenCodePermissionRule(action="*", resource="*", effect="allow")
    if not spec.interactive and not has_profile_native:
        # OpenCode run has nobody to answer its native default prompts. Preserve the
        # historical unrestricted child when the profile made no native decision.
        return OpenCodePermissionRule(action="*", resource="*", effect="allow")
    return None


def _headless_default_line(default: OpenCodePermissionRule | None) -> ReportLine:
    """What decides a headless ``approval: default`` spawn's unmatched calls.

    Not a policy element (``default`` approval has none), so it follows the elements. Which
    branch applies depends on the profile's other rules; the line makes that visible.
    """

    if default is not None:
        return ReportLine(
            "approval: default",
            "platform",
            f"permission *(*): {default.effect}; no profile rule names an OpenCode tool, so "
            "Meridian allows the calls a headless spawn has no one to approve",
        )
    return ReportLine(
        "approval: default",
        "native",
        "no permission *(*) rule, so OpenCode's own defaults decide unmatched calls; Meridian "
        "adds its allow-all fallback only when no profile rule names an OpenCode tool",
    )


def _tool_rule_lines(
    spec: ResolvedLaunchSpec,
    entries_by_rule: list[tuple[ToolRule, tuple[OpenCodePermissionRule, ...]]],
    final_by_key: dict[tuple[str, str], OpenCodePermissionRule],
    platform_denied_actions: frozenset[str],
    *,
    version: OpenCodeVersion,
    edit_conflict: ToolAction | None,
) -> list[ReportLine]:
    lines: list[ReportLine] = []
    for rule, entries in entries_by_rule:
        if not entries:
            continue
        rendered = ", ".join(f"{entry.action}({entry.resource})" for entry in entries)
        effective = _effective_action(rule.action, interactive=spec.interactive)
        if any(entry.action in platform_denied_actions for entry in entries):
            lines.append(
                ReportLine(rule_element(rule), "platform", "overridden to deny by a platform rule")
            )
            continue
        conflated = version == "v2" and any(entry.action == "edit" for entry in entries)
        if edit_conflict is not None and conflated:
            unchanged_edit_rule = (
                not _uses_v2_write_patch_alias(rule)
                and all(entry.resource == "*" for entry in entries)
                and effective == edit_conflict
            )
            if not unchanged_edit_rule:
                lines.append(
                    ReportLine(
                        rule_element(rule),
                        "partial",
                        "V2 conflates write/patch as edit; conflicting semantics "
                        f"conservatively use permission edit(*): {edit_conflict}",
                    )
                )
                continue
        if conflated and _uses_v2_write_patch_alias(rule):
            lines.append(
                ReportLine(
                    rule_element(rule),
                    "partial",
                    f"permission {rendered}: {effective}; V2 conflates write/patch as edit",
                )
            )
            continue
        beaten = [
            entry
            for entry in entries
            if final_by_key.get((entry.action, entry.resource), entry).effect != effective
        ]
        if beaten:
            lines.append(
                ReportLine(
                    rule_element(rule),
                    "partial",
                    f"permission {rendered}: {effective}; an identical stricter rule wins",
                )
            )
            continue
        if rule.action == "ask" and not spec.interactive:
            lines.append(
                ReportLine(
                    rule_element(rule),
                    "native",
                    f"permission {rendered}: deny; headless ask is refused",
                )
            )
        else:
            lines.append(
                ReportLine(rule_element(rule), "native", f"permission {rendered}: {effective}")
            )
    return lines


def compile_opencode_permissions(
    spec: ResolvedLaunchSpec,
) -> tuple[OpenCodePermissions, EnforcementReport]:
    """Compile ``spec.permission_policy`` to ordered config-agent rules and a report.

    The rules are spelled for ``spec.opencode_version``, the version bind resolved, which the
    config projection and the connection read too.
    """

    policy = spec.permission_policy
    resolved_version = bound_opencode_version(spec.opencode_version)
    entries_by_rule = [
        (
            rule,
            _rule_entries(
                rule,
                interactive=spec.interactive,
                version=resolved_version,
            ),
        )
        for rule in policy.rules
    ]
    has_profile_native = any(entries for _, entries in entries_by_rule)
    edit_conflict = (
        _v2_edit_conflict(entries_by_rule, interactive=spec.interactive)
        if resolved_version == "v2"
        else None
    )
    lines: list[ReportLine] = []
    profile_entries: list[OpenCodePermissionRule] = []
    default = _default_entry(spec, has_profile_native=has_profile_native)
    if default is not None:
        profile_entries.append(default)
    profile_entries.extend(entry for _, entries in entries_by_rule for entry in entries)
    if edit_conflict is not None:
        profile_entries = [entry for entry in profile_entries if entry.action != "edit"]
        profile_entries.append(
            OpenCodePermissionRule(action="edit", resource="*", effect=edit_conflict)
        )
    ordered = list(_collapse_exact_rules(profile_entries))

    platform_denied_actions: set[str] = set()
    for platform in policy.platform_rules:
        if platform.kind != "native-subagents":
            continue
        actions = tuple(
            _native_action(action, version=resolved_version)
            for capability in platform.capabilities
            for action in _PLATFORM_DENY_ACTIONS.get(capability, ())
        )
        platform_denied_actions.update(actions)
        if actions:
            denied = frozenset(actions)
            ordered = [entry for entry in ordered if entry.action not in denied]
            ordered.extend(
                OpenCodePermissionRule(action=action, resource="*", effect="deny")
                for action in actions
            )
            lines.append(
                ReportLine(
                    platform_element(platform),
                    "platform",
                    "permission " + ", ".join(actions) + ": deny",
                )
            )

    final_by_key = {(entry.action, entry.resource): entry for entry in ordered}

    for platform in policy.platform_rules:
        if platform.kind != "completion-commands":
            continue
        action = _native_action("bash", version=resolved_version)
        entries: list[OpenCodePermissionRule] = []
        kept: list[OpenCodePermissionRule] = []
        for command in platform.commands:
            pair = tuple(
                OpenCodePermissionRule(action=action, resource=resource, effect="allow")
                for resource in (command, f"{command} *")
            )
            entries.extend(pair)
            stricter = [
                existing
                for entry in pair
                if (existing := final_by_key.get((entry.action, entry.resource))) is not None
                and existing.effect != "allow"
            ]
            if stricter:
                # OpenCode's ``cmd *`` pattern also matches bare ``cmd``. Treat the two
                # spellings as one exception so either profile restriction covers both.
                strictest = max(stricter, key=lambda entry: _STRICTNESS[entry.effect])
                pair_keys = {(entry.action, entry.resource) for entry in pair}
                ordered = [
                    entry
                    for entry in ordered
                    if (entry.action, entry.resource) not in pair_keys
                ]
                replacements = [
                    OpenCodePermissionRule(
                        action=entry.action,
                        resource=entry.resource,
                        effect=strictest.effect,
                    )
                    for entry in pair
                ]
                ordered.extend(replacements)
                final_by_key.update(
                    {
                        (entry.action, entry.resource): entry
                        for entry in replacements
                    }
                )
                kept.extend(replacements)
                continue
            for entry in pair:
                if (entry.action, entry.resource) not in final_by_key:
                    ordered.append(entry)
                    final_by_key[(entry.action, entry.resource)] = entry
        rendered = ", ".join(
            f"{entry.action}({entry.resource})" for entry in entries
        )
        if kept:
            lines.append(
                ReportLine(
                    platform_element(platform),
                    "partial",
                    f"permission {rendered}: allow; an overlapping profile rule keeps its "
                    "command pair stricter",
                )
            )
        else:
            lines.append(
                ReportLine(
                    platform_element(platform),
                    "platform",
                    f"permission {rendered}: allow",
                )
            )

    lines.extend(
        _tool_rule_lines(
            spec,
            entries_by_rule,
            final_by_key,
            frozenset(platform_denied_actions),
            version=resolved_version,
            edit_conflict=edit_conflict,
        )
    )

    if default is not None and policy.approval != "default":
        note = f"permission *(*): {default.effect}"
        if policy.approval == "confirm" and not spec.interactive:
            note += "; a headless spawn cannot confirm, so calls are refused"
        lines.append(ReportLine(f"approval: {policy.approval}", "native", note))
    elif not spec.interactive and unscoped_action(policy.rules, "*") is None:
        lines.append(_headless_default_line(default))

    return tuple(ordered), complete_report(policy, lines, harness="OpenCode")


__all__ = [
    "OpenCodePermissionRule",
    "OpenCodePermissions",
    "compile_opencode_permissions",
]
