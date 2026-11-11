"""A continue or fork re-reads project-wide rules but never opens what the snapshot closed."""

from __future__ import annotations

import pytest

from meridian.lib.launch.policies import refresh_project_rules
from meridian.lib.tools import PROJECT_ORIGIN, ToolRule


def _rule(capability: str, action: str, *, pattern: str | None = None, origin: str | None = None):
    return ToolRule(
        capability=capability,
        pattern=pattern,
        action=action,  # type: ignore[arg-type]
        native=(capability,),
        origin=origin,
    )


def _project(capability: str, action: str, pattern: str | None = None) -> ToolRule:
    return _rule(capability, action, pattern=pattern, origin=PROJECT_ORIGIN)


def _keys(rules: tuple[ToolRule, ...]) -> list[tuple[str | None, str, str | None]]:
    return [(rule.capability, rule.action, rule.origin) for rule in rules]


def test_a_project_deny_always_refreshes() -> None:
    snapshot = (_rule("*", "deny"), _project("web", "deny"))
    bundle = (_rule("*", "deny"), _rule("read", "allow"), _project("bash", "deny"))

    assert _keys(refresh_project_rules(snapshot, bundle)) == [
        ("*", "deny", None),
        ("bash", "deny", PROJECT_ORIGIN),
    ]


@pytest.mark.parametrize("action", ["allow", "ask"])
def test_project_allow_or_ask_refreshes_only_for_the_shape_it_was_made_for(action: str) -> None:
    current = _project("bash", action)
    read = _rule("read", "allow")
    # `*` unchanged (none), `*: allow` unchanged: the bundle's rule is taken.
    assert refresh_project_rules((read,), (read, current)) == (read, current)
    star = _rule("*", "allow")
    assert refresh_project_rules((star,), (star, current)) == (star, current)
    # The agent gained `*: deny` since launch: the snapshot keeps what it launched with.
    closed = _rule("*", "deny")
    assert refresh_project_rules((read,), (closed, read, current)) == (read,)
    # `*: deny` with different allows: the bundle was shaped for another allowlist.
    snapshot = (closed, _rule("read", "allow"))
    bundle = (closed, _rule("read", "allow"), _rule("bash", "allow", pattern="rg *"), current)
    assert refresh_project_rules(snapshot, bundle) == snapshot
    # `*: deny` with the same allows: refreshed.
    same = (closed, _rule("read", "allow"), current)
    assert refresh_project_rules(snapshot, same) == (*snapshot, current)


def test_a_snapshot_keeps_its_own_project_allow_when_the_shape_changed() -> None:
    kept = _project("bash", "ask")
    snapshot = (_rule("read", "allow"), kept)
    bundle = (_rule("*", "deny"), _rule("read", "allow"), _project("bash", "allow"))

    assert refresh_project_rules(snapshot, bundle) == snapshot
