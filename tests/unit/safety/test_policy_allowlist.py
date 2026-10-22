"""The allowlist queries every harness compile builds on: C4-C7 ask these, never re-derive."""

from __future__ import annotations

import pytest

from meridian.lib.safety.policy import build_permission_policy
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
    ("rules", "whole_policy", "capabilities", "allowed"),
    [
        # Scoped allows with no unscoped `bash` rule: only those commands may run.
        (_SESSION_MINER, False, ("bash",), (_RG, _SESSION)),
        # An unscoped rule for the capability decides every call, so nothing is implied.
        ((*_SESSION_MINER, tool_rule("bash", "ask", "Bash")), False, (), (_RG, _SESSION, "Bash")),
        # A `*` rule decides every unmatched call; `*: deny` makes the whole policy an allowlist.
        ((*_SESSION_MINER, tool_rule("*", "allow")), False, (), (_RG, _SESSION)),
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
    capabilities: tuple[str, ...],
    allowed: tuple[str, ...],
) -> None:
    policy = build_permission_policy(rules=rules, sandbox=None)

    assert policy.is_allowlist is whole_policy
    assert policy.allowlisted_capabilities == capabilities
    assert policy.allowed_natives == allowed
