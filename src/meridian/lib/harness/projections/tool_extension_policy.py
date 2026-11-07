"""The tool-rule compile shared by harnesses that enforce scoped rules in their own process.

Pi's ``tool_call`` handler and Codex's ``PreToolUse`` hook take the same rules and the same
fail-closed decision; each harness owns only how the policy reaches its enforcer.
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict

from meridian.lib.harness.permission_broker import HEADLESS_REJECT_REASON
from meridian.lib.safety.policy import PermissionPolicy, PlatformCommand
from meridian.lib.tools import ToolAction, ToolRule, split_native

# Carries one launch's ExtensionPolicy as JSON to its enforcer. Compiled afresh per launch,
# so a nested launch never inherits it (see ``child=False`` in env_registry).
TOOL_POLICY_ENV = "_MERIDIAN_TOOL_POLICY"
# What a Pi bundle built before the rename reads. Pi's adapter sets both for one release.
# Delete this, its use in pi.py, and its entries in constants.py and env_registry.py once
# no supported Pi bundle predates `_MERIDIAN_TOOL_POLICY` (the release after the one that
# renamed it).
LEGACY_PI_POLICY_ENV = "_MERIDIAN_PI_PERMISSION_POLICY"
_STRICTNESS: dict[ToolAction, int] = {"allow": 0, "ask": 1, "deny": 2}


class ExtensionRule(BaseModel):
    """One native tool rule evaluated by a harness-side permission enforcer."""

    model_config = ConfigDict(frozen=True)

    capability: str
    tool: str
    pattern: str | None = None
    action: ToolAction
    label: str


class ExtensionPolicy(BaseModel):
    """Launch-local input for a harness-side permission enforcer: Pi's ``tool_call`` handler
    and Codex's ``PreToolUse`` hook."""

    model_config = ConfigDict(frozen=True)

    version: int = 1
    rules: tuple[ExtensionRule, ...]
    meridian_commands: tuple[PlatformCommand, ...] = ()
    headless_reject_reason: str = HEADLESS_REJECT_REASON
    # Whether the handler refuses a bash command it cannot split into simple commands.
    bash_fail_closed: bool = False


def extension_rules(rules: tuple[ToolRule, ...]) -> tuple[ExtensionRule, ...]:
    projected: list[ExtensionRule] = []
    for rule in rules:
        if rule.is_wildcard:
            projected.append(
                ExtensionRule(
                    capability="*",
                    tool="*",
                    action=rule.action,
                    label="*",
                )
            )
            continue
        if rule.native_status != "known":
            continue
        for native in rule.native:
            tool, pattern = split_native(native)
            projected.append(
                ExtensionRule(
                    capability=rule.capability or tool,
                    tool=tool,
                    pattern=pattern,
                    action=rule.action,
                    label=native,
                )
            )
    return tuple(projected)


def command_grants(policy: PermissionPolicy) -> tuple[PlatformCommand, ...]:
    return tuple(
        command
        for platform in policy.platform_rules
        if platform.kind == "meridian-commands"
        for command in platform.commands
    )


def strictest(rules: Iterable[ExtensionRule]) -> ToolAction | None:
    """The strictest action among ``rules``, as the extension ranks equally specific rules."""

    return max((rule.action for rule in rules), key=_STRICTNESS.__getitem__, default=None)


def bash_fails_closed(rules: tuple[ExtensionRule, ...]) -> bool:
    """Whether a bash command the handler cannot split must be refused.

    It must when such a command could be refused or asked for without matching a scoped
    rule: the rule deciding unmatched commands (bash's own unscoped rule, else ``*``'s) is
    ``deny`` or ``ask``, as in every allowlist, or a scoped ``ask`` could match it. Under an
    unscoped allow, or no rule, only a scoped deny refuses, so that check is best effort: a
    command the splitter cannot read runs.
    """

    shell = tuple(rule for rule in rules if rule.tool in {"bash", "*"})
    if any(rule.pattern is not None and rule.action == "ask" for rule in shell):
        return True
    for tool in ("bash", "*"):
        unmatched = strictest(rule for rule in shell if rule.tool == tool and rule.pattern is None)
        if unmatched is not None:
            return unmatched != "allow"
    return False


def needs_extension(
    rules: tuple[ExtensionRule, ...], *, meridian_commands: tuple[PlatformCommand, ...]
) -> bool:
    # Meridian commands need the extension only when a rule could otherwise restrict the shell.
    return bool(
        (meridian_commands and rules)
        or any(
            rule.pattern is not None
            or rule.action == "ask"
            or (rule.tool == "*" and rule.action == "deny")
            for rule in rules
        )
    )


__all__ = [
    "LEGACY_PI_POLICY_ENV",
    "TOOL_POLICY_ENV",
    "ExtensionPolicy",
    "ExtensionRule",
    "bash_fails_closed",
    "command_grants",
    "extension_rules",
    "needs_extension",
    "strictest",
]
