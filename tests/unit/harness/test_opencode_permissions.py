"""The OpenCode launch rules never answer a call more loosely than the native agent file.

A small last-match evaluator reads the real ``compile_opencode_permissions`` output (V1's
rendered map, V2's rule list) and holds it to three things per row: at least as strict as the
profile, at least as strict as the override's own last-match reading, and equal to the profile
when there is no override. A new pair is one row.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from fnmatch import fnmatchcase
from itertools import product
from pathlib import Path
from typing import Any, cast

import pytest

from meridian.lib.harness.opencode_backend import OpenCodeVersion
from meridian.lib.harness.projections.permissions_opencode import compile_opencode_permissions
from meridian.lib.harness.projections.project_opencode_config import _project_v1_permissions
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.policy import PermissionPolicy, PlatformCommand, PlatformRule
from meridian.lib.tools import STRICTNESS, ToolAction, ToolRule, strictest

_FIXTURES = json.loads(
    (Path(__file__).parents[2] / "fixtures" / "tool_policy_cases.json").read_text()
)
_V2_ALIASES = {"bash": "shell", "write": "edit", "patch": "edit", "task": "subagent"}
_ACTIONS = ("bash", "edit", "read", "webfetch", "task")
_RESOURCES = ("git status", "git push x", "uname", "rm x", "a.txt", "x.env")

type Rules = list[tuple[str, str, ToolAction]]  # (action, resource, effect), last match wins


def _matches(pattern: str, value: str) -> bool:
    # OpenCode lets a trailing " *" also match the bare command.
    return fnmatchcase(value, pattern) or (
        pattern.endswith(" *") and fnmatchcase(value, pattern[:-2])
    )


def _last_match(rules: Rules, action: str, resource: str, baseline: ToolAction) -> ToolAction:
    answer = baseline
    for rule_action, rule_resource, effect in rules:
        if _matches(rule_action, action) and _matches(rule_resource, resource):
            answer = effect
    return answer


def _stricter(a: ToolAction, b: ToolAction) -> bool:
    return STRICTNESS[a] >= STRICTNESS[b]


def _native(action: str, version: str) -> str:
    return _V2_ALIASES.get(action, action) if version == "v2" else action


def _flatten_v1(projected: dict[str, object]) -> Rules:
    rules: Rules = []
    for action, value in projected.items():
        if isinstance(value, str):
            rules.append((action, "*", cast("ToolAction", value)))
        else:
            rules.extend(
                (action, resource, cast("ToolAction", effect))
                for resource, effect in cast("dict[str, str]", value).items()
            )
    return rules


def _launch_rules(spec: ResolvedLaunchSpec, version: str) -> Rules:
    compiled, _report = compile_opencode_permissions(spec)
    if version == "v1":
        return _flatten_v1(_project_v1_permissions(compiled))
    return [(rule.action, rule.resource, rule.effect) for rule in compiled]


def _split(native: str) -> tuple[str, str]:
    name, _, rest = native.partition("(")
    return name, rest[:-1] if rest else "*"


def _profile_answer(
    profile: list[tuple[str, ToolAction]],
    grants: list[tuple[str, bool]],
    approval: str,
    interactive: bool,
    action: str,
    resource: str,
    baseline: ToolAction,
) -> ToolAction:
    """What the native file answers: the strictest named rule, else the profile's fallback."""

    def eff(a: ToolAction) -> ToolAction:
        return "deny" if a == "ask" and not interactive else a

    if action == "bash" and any(
        resource == cmd or (not exact and resource.startswith(cmd + " ")) for cmd, exact in grants
    ):
        return "allow"
    named = [
        eff(effect)
        for native, effect in profile
        if native != "*"
        and _matches(_split(native)[0], action)
        and _matches(_split(native)[1], resource)
    ]
    fallback = strictest(eff(effect) for native, effect in profile if native == "*")
    if fallback is None:
        if approval == "confirm":
            fallback = "ask" if interactive else "deny"
        elif approval in {"auto", "never", "yolo"} or (
            approval == "default" and not interactive and not profile
        ):
            fallback = "allow"
    answer = strictest(named) or fallback
    # A fallback of allow or none writes no restricting `*`: the outside rules decide.
    if answer == "allow" and fallback is None:
        return baseline
    return answer or baseline


def _override_answer(
    override: dict[str, Any],
    interactive: bool,
    version: str,
    action: str,
    resource: str,
) -> ToolAction:
    """The author's last-match reading, in author order; no rule for a call reads as allow."""

    rules: Rules = []
    for key, value in override.items():
        entries = {"*": value} if isinstance(value, str) else value
        for res, effect in entries.items():
            effect = "deny" if effect == "ask" and not interactive else effect
            rules.append((_native(key, version), res, effect))
    return _last_match(rules, _native(action, version), resource, "allow")


def _spec(
    profile: list[tuple[str, ToolAction]],
    *,
    approval: str,
    interactive: bool,
    version: str,
    override: dict[str, Any] | None,
    grants: list[tuple[str, bool]],
) -> ResolvedLaunchSpec:
    rules = tuple(
        ToolRule(capability="*", action=effect)
        if native == "*"
        else ToolRule(
            capability=_split(native)[0],
            pattern=None if "(" not in native else _split(native)[1],
            action=effect,
            native=(native,),
        )
        for native, effect in profile
    )
    platform = (
        PlatformRule(
            kind="meridian-commands",
            commands=tuple(
                PlatformCommand(command=cmd, match="exact" if exact else "prefix")
                for cmd, exact in grants
            ),
        ),
    )
    return ResolvedLaunchSpec(
        opencode_version=cast("OpenCodeVersion", version),
        interactive=interactive,
        permission_policy=PermissionPolicy(
            rules=rules,
            platform_rules=platform if grants else (),
            approval=cast("Any", approval),
        ),
        native_config={"permission": override} if override is not None else {},
    )


# (id, profile, approval, override in author order)
_ROWS: list[tuple[str, list[tuple[str, ToolAction]], str, dict[str, Any] | None]] = [
    ("override *: allow keeps approval", [], "confirm", {"*": "allow"}),
    ("override *: allow over a profile deny", [("bash(rm *)", "deny")], "default", {"*": "allow"}),
    ("override ask * raises its own rule", [], "default", {"webfetch": "ask", "*": "deny"}),
    ("override * before its rule", [], "default", {"*": "deny", "webfetch": "ask"}),
    ("override map under *", [], "confirm", {"*": {"*": "deny"}}),
    (
        "override map under * with a resource",
        [("bash(git *)", "allow"), ("*", "deny")],
        "default",
        {"*": {"git *": "deny"}},
    ),
    ("override * ask raises a profile allow", [("bash(git *)", "allow")], "never", {"*": "ask"}),
    (
        "override * ask over profile allowlist",
        [("*", "deny"), ("bash(git *)", "allow")],
        "default",
        {"*": "ask"},
    ),
    ("override rule under profile *: deny", [("*", "deny")], "default", {"webfetch": "ask"}),
    ("override allow carve-out ignored", [("*", "deny")], "default", {"bash": {"git *": "allow"}}),
    ("override glob key", [("bash(git *)", "allow")], "never", {"w*": "ask"}),
    ("override read deny under never", [("bash(date *)", "deny")], "never", {"read": "deny"}),
    (
        "pr c1: allow with no restricting *",
        [("bash(uname *)", "allow"), ("edit", "deny")],
        "default",
        None,
    ),
    ("pr c2: never with a deny", [("bash(date *)", "deny")], "never", None),
    ("pr c3: never, rm denied", [("bash(rm *)", "deny")], "never", None),
    ("approval confirm allowlist", [("*", "deny"), ("read", "allow")], "confirm", None),
    (
        "scoped deny over allow",
        [("bash", "allow"), ("bash(rm *)", "deny"), ("*", "deny")],
        "default",
        None,
    ),
    ("ask rule", [("bash(git push *)", "ask"), ("*", "ask")], "default", None),
]


@pytest.mark.parametrize("baseline", ["allow", "deny"])
@pytest.mark.parametrize("version", ["v1", "v2"])
@pytest.mark.parametrize("interactive", [True, False])
@pytest.mark.parametrize(("_id", "profile", "approval", "override"), _ROWS)
def test_launch_is_no_looser_than_profile_or_override(
    _id: str,
    profile: list[tuple[str, ToolAction]],
    approval: str,
    override: dict[str, Any] | None,
    interactive: bool,
    version: str,
    baseline: str,
) -> None:
    _check(
        profile,
        approval,
        override,
        interactive,
        version,
        cast("ToolAction", baseline),
        [],
        product(_ACTIONS, _RESOURCES),
    )


def test_fixture_policies_hold() -> None:
    """The Pi/Codex evaluator's policies and commands, asked of the OpenCode launch."""

    for name, policy in _FIXTURES["policies"].items():
        profile: list[tuple[str, ToolAction]] = [
            ("*" if rule["capability"] == "*" else _fixture_native(rule), rule["action"])
            for rule in policy["rules"]
        ]
        grants = [(c["command"], c["match"] == "exact") for c in policy["meridian_commands"]]
        commands = sorted({case["command"] for case in _FIXTURES["cases"]})
        for version, interactive, baseline in product(
            ("v1", "v2"), (True, False), ("allow", "deny")
        ):
            _check(
                profile,
                "default",
                None,
                interactive,
                version,
                cast("ToolAction", baseline),
                grants,
                product(("bash",), commands),
                label=name,
            )


def _fixture_native(rule: dict[str, Any]) -> str:
    return "bash" if rule["pattern"] is None else f"bash({rule['pattern']})"


def _check(
    profile: list[tuple[str, ToolAction]],
    approval: str,
    override: dict[str, Any] | None,
    interactive: bool,
    version: str,
    baseline: ToolAction,
    grants: list[tuple[str, bool]],
    calls: Iterable[tuple[str, str]],
    label: str = "",
) -> None:
    spec = _spec(
        profile,
        approval=approval,
        interactive=interactive,
        version=version,
        override=override,
        grants=grants,
    )
    launch = _launch_rules(spec, version)
    for action, resource in calls:
        got = _last_match(launch, _native(action, version), resource, baseline)
        want = _profile_answer(profile, grants, approval, interactive, action, resource, baseline)
        context = f"{label} {action}({resource}) got {got}, profile {want}, rules {launch}"
        assert _stricter(got, want), f"looser than the profile: {context}"
        if override is None:
            assert got == want, f"differs from the profile: {context}"
        else:
            own = _override_answer(override, interactive, version, action, resource)
            assert _stricter(got, own), f"looser than the override: {context}"
