"""The tool-rule compile shared by harnesses that enforce scoped rules in their own process.

Pi's ``tool_call`` handler and Codex's ``PreToolUse`` hook both ask one evaluator
(``tool_policy_runtime/evaluate.py``) and take the same rules; each harness owns only how the
policy and the evaluator's command line reach it.
"""

from __future__ import annotations

import sys
from functools import cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from meridian.lib.harness.permission_broker import HEADLESS_REJECT_REASON
from meridian.lib.safety.policy import PermissionPolicy, PlatformCommand
from meridian.lib.tools import ToolAction, ToolRule, split_native, strictest

# Carries one launch's ExtensionPolicy as JSON to its enforcer. Compiled afresh per launch,
# so a nested launch never inherits it (see ``child=False`` in env_registry).
TOOL_POLICY_ENV = "_MERIDIAN_TOOL_POLICY"
# Carries the evaluator's argv (a JSON list) to the Pi extension, which cannot name the
# interpreter Meridian runs on.
TOOL_EVALUATOR_ENV = "_MERIDIAN_TOOL_EVALUATOR"
_EVALUATOR = Path(__file__).resolve().parents[3] / "tool_policy_runtime" / "evaluate.py"
# What a Pi bundle built before the rename reads. Pi's adapter sets both for one release.
# Delete this, its use in pi.py, and its entries in constants.py and env_registry.py once
# no supported Pi bundle predates `_MERIDIAN_TOOL_POLICY` (the release after the one that
# renamed it).
LEGACY_PI_POLICY_ENV = "_MERIDIAN_PI_PERMISSION_POLICY"


@cache
def evaluator_argv() -> tuple[str, ...]:
    """Run the evaluator with the launching interpreter; ``-I`` keeps an agent-writable
    environment from steering its imports."""

    return (sys.executable, "-I", str(_EVALUATOR))


class ExtensionRule(BaseModel):
    """One native tool rule evaluated by a harness-side permission enforcer."""

    model_config = ConfigDict(frozen=True)

    capability: str
    tool: str
    pattern: str | None = None
    action: ToolAction
    label: str


class ExtensionPolicy(BaseModel):
    """Launch-local input for the tool-policy evaluator, asked by Pi's ``tool_call`` handler and
    Codex's ``PreToolUse`` hook."""

    model_config = ConfigDict(frozen=True)

    version: int = 2
    rules: tuple[ExtensionRule, ...]
    meridian_commands: tuple[PlatformCommand, ...] = ()
    headless_reject_reason: str = HEADLESS_REJECT_REASON
    # Whether the evaluator refuses a bash command it cannot split into simple commands.
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
        unmatched = strictest(
            rule.action for rule in shell if rule.tool == tool and rule.pattern is None
        )
        if unmatched is not None:
            return unmatched != "allow"
    return False


def needs_extension(
    rules: tuple[ExtensionRule, ...], *, meridian_commands: tuple[PlatformCommand, ...]
) -> bool:
    """Whether a policy needs an in-process shell enforcer.

    Allows restrict nothing, so rules that are all allows need none. Meridian commands need
    it only when a rule could otherwise restrict the shell.
    """

    if all(rule.action == "allow" for rule in rules):
        return False
    return bool(
        meridian_commands
        or any(
            rule.pattern is not None
            or rule.action == "ask"
            or (rule.tool == "*" and rule.action == "deny")
            for rule in rules
        )
    )


__all__ = [
    "LEGACY_PI_POLICY_ENV",
    "TOOL_EVALUATOR_ENV",
    "TOOL_POLICY_ENV",
    "ExtensionPolicy",
    "ExtensionRule",
    "bash_fails_closed",
    "command_grants",
    "evaluator_argv",
    "extension_rules",
    "needs_extension",
]
