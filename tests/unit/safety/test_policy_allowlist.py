"""The allowlist facts every harness compile builds on: C4-C7 read these, never re-derive."""

from __future__ import annotations

import pytest

from meridian.lib.safety.policy import (
    ALLOWLIST_ORIGIN,
    Allow,
    EffectiveNetworkFacts,
    Hosts,
    build_permission_policy,
    network_policy_name,
    parse_network_policy,
)
from meridian.lib.tools import ToolRule
from tests.support.launch import tool_rule

_RG = "Bash(rg *)"
_SESSION = "Bash(meridian session *)"
_SESSION_MINER = (
    tool_rule("bash", "allow", _RG, pattern="rg *"),
    tool_rule("bash", "allow", _SESSION, pattern="meridian session *"),
    tool_rule("bash", "deny", "Bash(git stash *)", pattern="git stash *"),
    tool_rule("edit", "deny", "Edit", "Write"),
)


@pytest.mark.parametrize(
    ("rules", "whole_policy", "denied", "allowed"),
    [
        # Scoped allows with no unscoped `bash` rule: only those commands may run, so the
        # policy writes the unscoped deny that means, naming the scoped rules' tools.
        (_SESSION_MINER, False, (("bash", ("Bash",)),), ("Bash",)),
        # An unscoped rule for the capability decides every call, so nothing is implied.
        ((*_SESSION_MINER, tool_rule("bash", "ask", "Bash")), False, (), ("Bash",)),
        # A `*` rule decides every unmatched call; `*: deny` makes the whole policy an allowlist.
        ((*_SESSION_MINER, tool_rule("*", "allow")), False, (), ("Bash",)),
        (
            (
                tool_rule("*", "deny"),
                tool_rule("read", "allow", "Read"),
                tool_rule("grep", "ask", "Grep"),
            ),
            True,
            (),
            ("Read", "Grep"),
        ),
        # Scoped denies alone restrict nothing else.
        ((tool_rule("bash", "deny", "Bash(git stash *)", pattern="git stash *"),), False, (), ()),
    ],
)
def test_allowlist_queries(
    rules: tuple[ToolRule, ...],
    whole_policy: bool,
    denied: tuple[tuple[str, tuple[str, ...]], ...],
    allowed: tuple[str, ...],
) -> None:
    policy = build_permission_policy(
        rules=rules,
        network_facts=EffectiveNetworkFacts.from_values(sandbox=None, network=None),
    )

    written = policy.rules[: len(policy.rules) - len(rules)]
    assert policy.rules[len(written) :] == rules
    assert all(rule.origin == ALLOWLIST_ORIGIN and rule.action == "deny" for rule in written)
    assert all(rule.pattern is None for rule in written)
    assert policy.is_allowlist is whole_policy
    assert tuple((rule.capability, rule.native) for rule in written) == denied
    assert policy.allowed_tools == allowed


def test_network_default_follows_sandbox_without_losing_authored_allow() -> None:
    native_default = build_permission_policy(
        network_facts=EffectiveNetworkFacts.from_values(sandbox=None, network=None)
    )
    sandboxed_default = build_permission_policy(
        network_facts=EffectiveNetworkFacts.from_values(
            sandbox="workspace-write", network=None
        )
    )
    authored = build_permission_policy(
        network_facts=EffectiveNetworkFacts.from_values(
            sandbox="workspace-write", network=parse_network_policy("allow")
        )
    )

    assert (native_default.network, network_policy_name(native_default.network)) == (
        Allow(),
        "allow",
    )
    assert (sandboxed_default.network, network_policy_name(sandboxed_default.network)) == (
        Hosts(),
        "deny",
    )
    assert (authored.network, network_policy_name(authored.network)) == (Allow(), "allow")
