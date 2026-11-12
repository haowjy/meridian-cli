"""Compile Meridian's permission policy to ordered OpenCode agent rules.

OpenCode resolves the last matching rule, and the launch config lands after the agent file
and ``opencode.json``, so these rules decide every call they match. Meridian's policy resolves
the strictest match, so the compile orders the default first, then every allow, every ask and
every deny (platform denies included), and Meridian's own command grants last: a grant is
decided before any rule. How narrow a key is never matters.

The rule, the same one Mars states for the ``permission`` map of a native agent file: for
every call the rules answer at least as strictly as the profile's tool rules and as the
author's ``harness-overrides.opencode.permission``, and exactly as the tool rules when there is
no override. Three consequences:

- A profile allow is written only under a restricting ``*`` (the profile's, the approval
  default or the override's), where it carves an exception. Elsewhere it restates OpenCode's
  default and could only override ``opencode.json``; the dry-run says so.
- The override adds restrictions only: the Mars bundle hands it over with its keys sorted,
  so the author's order is gone and an allow could be a carve-out or a mistake. An ignored
  allow can only make the launch stricter than the native file. The override's ``*`` is a
  floor: an ask or deny raises every rule from both sources. The final ``*`` is the stricter
  of that floor and the profile's fallback (its ``*``, else the approval default).
- A shell rule that restricts also denies ``execute`` (code mode), as the native file does.

Because V2 merges ``write`` and ``patch`` into ``edit``, rules for those capabilities share
one permission and the stricter wins. The config projector only renders this typed result as
the V1 nested ``permission`` map or the V2 ``permissions`` rule list; nothing else decides
OpenCode permissions.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import cast

from meridian.lib.harness.opencode_backend import OpenCodeVersion, bound_opencode_version
from meridian.lib.harness.projections.compaction import compaction_report
from meridian.lib.harness.projections.effort import effort_report
from meridian.lib.harness.projections.native_config import unsupported_native_config_report
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.policy import (
    EnforcementReport,
    ReportLine,
    complete_report,
    platform_element,
    rule_element,
)
from meridian.lib.tools import (
    PLATFORM_ORIGIN,
    STRICTNESS,
    ToolAction,
    ToolRule,
    split_native,
    strictest,
)

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


def _order(rules: list[OpenCodePermissionRule]) -> tuple[OpenCodePermissionRule, ...]:
    """The default first, then allows, asks and denies; the strictest of one key survives.

    Position is the whole precedence, and V1 renders a map where a repeated key keeps its first
    position, so a key appears once, at its strictest effect.
    """

    strongest: dict[tuple[str, str], OpenCodePermissionRule] = {}
    for rule in rules:
        key = (rule.action, rule.resource)
        current = strongest.get(key)
        if current is None or STRICTNESS[rule.effect] > STRICTNESS[current.effect]:
            strongest[key] = rule
    return tuple(
        sorted(
            strongest.values(),
            key=lambda rule: (rule.action != "*", STRICTNESS[rule.effect]),
        )
    )


@dataclass(frozen=True)
class _Override:
    """The restrictions of the author's ``harness-overrides.opencode.permission``."""

    rules: tuple[OpenCodePermissionRule, ...]
    ignored: tuple[str, ...]

    @property
    def star(self) -> ToolAction | None:
        """The override's ``*`` rule: a floor under every other rule, never a fallback."""

        return strictest(rule.effect for rule in self.rules if rule.action == "*")


def _override(spec: ResolvedLaunchSpec, *, version: OpenCodeVersion) -> _Override:
    """Read the override as restrictions only.

    Interim derivation. The bundle sorts the map's keys, so the author's order is gone and
    OpenCode's last-match reading cannot be applied. Every ask and deny is kept and allows
    (``*: allow`` included) are ignored: with the strictest match deciding, the result is at
    least as strict as the author's own reading. A glob action key (``sh*``) can match another
    key's calls, which one ordering of keys cannot make safe, so it becomes a deny of
    everything it names. Do not extend this toward Mars's order-aware pipeline: the fix is the
    Mars bundle carrying the computed rule list (mars-agents issue #374), after which this
    reading, ``_join`` and ``_raised`` are deleted.
    """

    permission = spec.native_config.get("permission")
    if permission is None:
        return _Override((), ())
    if not isinstance(permission, Mapping):
        return _Override((), ("permission is not a map",))
    ignored: list[str] = []
    rules: list[OpenCodePermissionRule] = []
    for key, value in cast("Mapping[str, object]", permission).items():
        entries = (
            {"*": value}
            if isinstance(value, str)
            else cast("Mapping[str, object]", value)
            if isinstance(value, Mapping)
            else None
        )
        if entries is None:
            ignored.append(f"{key}: unreadable")
            continue
        action = _native_action(key, version=version)
        for resource, raw in entries.items():
            if not isinstance(raw, str) or raw not in STRICTNESS:
                ignored.append(f"{key}: unreadable")
                continue
            effect = _effective_action(raw, interactive=spec.interactive)
            if effect == "allow":
                ignored.append(f"{key}({resource}): allow")
            elif action == "*":
                # A map under ``*`` names resources of every action. V1's rendered map cannot
                # order such a rule after the named ones, so it applies to all calls.
                if resource != "*":
                    ignored.append(f"*({resource}): applied to every call")
                rules.append(OpenCodePermissionRule(action="*", resource="*", effect=effect))
            elif "*" in action:
                ignored.append(f"{key}: glob key denied")
                rules.append(OpenCodePermissionRule(action=action, resource="*", effect="deny"))
            else:
                rules.append(
                    OpenCodePermissionRule(action=action, resource=resource, effect=effect)
                )
    return _Override(tuple(rules), tuple(dict.fromkeys(ignored)))


def _override_report(spec: ResolvedLaunchSpec, override: _Override) -> EnforcementReport:
    if "permission" not in spec.native_config:
        return ()
    if override.ignored:
        return (
            ReportLine(
                "native_config: permission",
                "partial",
                "restrictions applied, strictest match wins; ignored: "
                + ", ".join(override.ignored),
            ),
        )
    return (
        ReportLine(
            "native_config: permission",
            "native",
            "restrictions applied, strictest match wins (the bundle keeps no key order)",
        ),
    )


def _raised(effect: ToolAction, floor: ToolAction | None) -> ToolAction:
    return effect if floor is None else max(effect, floor, key=STRICTNESS.__getitem__)


def _join(
    policy_rules: list[OpenCodePermissionRule],
    override: _Override,
    star: ToolAction | None,
) -> list[OpenCodePermissionRule]:
    """Merge profile and override rules under the final ``*``.

    A profile rule is raised to the override's ``*``; an override rule is raised to the final
    ``*``, which also holds the profile's fallback. A profile allow survives only under a
    restricting ``*``, which it then carves an exception to.
    """

    joined: list[OpenCodePermissionRule] = []
    for rule in policy_rules:
        effect = _raised(rule.effect, override.star)
        if effect != "allow" or star not in {None, "allow"}:
            joined.append(replace(rule, effect=effect))
    joined.extend(replace(rule, effect=_raised(rule.effect, star)) for rule in override.rules)
    return joined


def _uses_v2_write_patch_alias(rule: ToolRule) -> bool:
    return rule.capability in {"write", "patch"} or any(
        split_native(native)[0] in {"write", "patch"} for native in rule.native
    )


def _default_entry(
    spec: ResolvedLaunchSpec, *, has_profile_native: bool, has_star: bool
) -> OpenCodePermissionRule | None:
    """Return the approval/fallback rule when no explicit ``*`` rule decides it."""

    policy = spec.permission_policy
    if has_star:
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
    *,
    version: OpenCodeVersion,
) -> list[ReportLine]:
    lines: list[ReportLine] = []
    for rule, entries in entries_by_rule:
        if not entries:
            continue
        rendered = ", ".join(f"{entry.action}({entry.resource})" for entry in entries)
        effective = _effective_action(rule.action, interactive=spec.interactive)
        if any((entry.action, entry.resource) not in final_by_key for entry in entries):
            lines.append(
                ReportLine(
                    rule_element(rule),
                    "partial",
                    f"not written ({rendered}: {effective}): no restricting `*` applies, so "
                    "the project's opencode.json and OpenCode's own rules decide",
                )
            )
        elif any(final_by_key[(e.action, e.resource)].effect != effective for e in entries):
            lines.append(
                ReportLine(
                    rule_element(rule),
                    "partial",
                    f"permission {rendered}: {effective}; a stricter rule wins (another rule "
                    "on the same permission, the override's *, or a Meridian command grant)",
                )
            )
        elif (
            version == "v2"
            and any(entry.action == "edit" for entry in entries)
            and (_uses_v2_write_patch_alias(rule))
        ):
            lines.append(
                ReportLine(
                    rule_element(rule),
                    "partial",
                    f"permission {rendered}: {effective}; V2 conflates write/patch as edit",
                )
            )
        elif rule.action == "ask" and not spec.interactive:
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
    has_profile_native = any(
        entries for rule, entries in entries_by_rule if rule.origin != PLATFORM_ORIGIN
    )
    override = _override(spec, version=resolved_version)
    lines: list[ReportLine] = []
    rule_entries = [entry for _rule, entries in entries_by_rule for entry in entries]
    policy_star = strictest(entry.effect for entry in rule_entries if entry.action == "*")
    default = _default_entry(
        spec, has_profile_native=has_profile_native, has_star=policy_star is not None
    )
    # The profile's fallback is its ``*`` else the approval default; the override's ``*`` only
    # restricts it. The override never decides whether the approval default is written.
    star = strictest(
        effect
        for effect in (policy_star, override.star, default.effect if default else None)
        if effect is not None
    )
    named = [entry for entry in rule_entries if entry.action != "*"]
    if resolved_version == "v2" and any(
        entry.action == "shell" and entry.effect != "allow" for entry in named
    ):
        # Code mode (``execute``) ignores shell rules; the native agent file denies it too.
        named.append(OpenCodePermissionRule(action="execute", resource="*", effect="deny"))
    joined = _join(named, override, star)
    star_rule = [OpenCodePermissionRule(action="*", resource="*", effect=star)] if star else []
    grants: list[OpenCodePermissionRule] = []
    for platform in policy.platform_rules:
        if platform.kind != "meridian-commands":
            continue
        action = _native_action("bash", version=resolved_version)
        entries = tuple(
            OpenCodePermissionRule(action=action, resource=resource, effect="allow")
            for command in platform.commands
            for resource in (
                (command.command,)
                if command.match == "exact"
                else (command.command, f"{command.command} *")
            )
        )
        grants.extend(entries)
        rendered = ", ".join(f"{entry.action}({entry.resource})" for entry in entries)
        lines.append(
            ReportLine(platform_element(platform), "platform", f"permission {rendered}: allow")
        )
    grant_keys = {(grant.action, grant.resource) for grant in grants}
    ordered = [
        entry
        for entry in _order([*star_rule, *joined])
        if (entry.action, entry.resource) not in grant_keys
    ]
    ordered.extend(dict.fromkeys(grants))
    final_by_key = {(entry.action, entry.resource): entry for entry in ordered}

    lines.extend(_tool_rule_lines(spec, entries_by_rule, final_by_key, version=resolved_version))

    if default is not None and policy.approval != "default":
        note = f"permission *(*): {star}"
        if star != default.effect:
            note += "; the override's * is stricter"
        if policy.approval == "confirm" and not spec.interactive:
            note += "; a headless spawn cannot confirm, so calls are refused"
        lines.append(ReportLine(f"approval: {policy.approval}", "native", note))
    elif not spec.interactive and policy_star is None and override.star is None:
        lines.append(_headless_default_line(default))

    lines.extend(_override_report(spec, override))
    lines.extend(
        unsupported_native_config_report(
            {key: value for key, value in spec.native_config.items() if key != "permission"},
            harness="OpenCode",
        )
    )
    lines.extend(effort_report(spec, harness="OpenCode"))
    lines.extend(compaction_report(spec, harness="OpenCode"))
    return tuple(ordered), complete_report(policy, lines, harness="OpenCode")


__all__ = [
    "OpenCodePermissionRule",
    "OpenCodePermissions",
    "compile_opencode_permissions",
]
