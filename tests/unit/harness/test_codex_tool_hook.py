"""The Codex PreToolUse enforcer's decision table, shared with the Pi extension."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

import meridian


def _load_enforcer() -> ModuleType:
    path = Path(meridian.__file__).parent / "codex_runtime" / "tool_hook.py"
    spec = importlib.util.spec_from_file_location("codex_tool_hook_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through it
    spec.loader.exec_module(module)
    return module


_hook = _load_enforcer()


_FIXTURE = Path(__file__).parents[2] / "fixtures" / "tool_policy_cases.json"
_CASES = json.loads(_FIXTURE.read_text())


def _slow_pattern_policy() -> dict[str, object]:
    rule = {
        "capability": "bash",
        "tool": "bash",
        "pattern": "git * reset * x * --hard",
        "action": "deny",
        "label": "bash(git * reset * x * --hard)",
    }
    return {"rules": [rule], "meridian_commands": [], "bash_fail_closed": False}


@pytest.mark.parametrize(
    ("policy", "command", "denied"),
    [
        *(
            (_CASES["policies"][case["policy"]], case["command"], case["expected"] == "deny")
            for case in _CASES["cases"]
        ),
        # The hook splits on a lone `&`; Pi's extension leaves it unchecked without fail-closed.
        (_CASES["policies"]["deny_list"], "sleep 1 & git reset --hard", True),
        # A pattern with many interior stars must not make the match slow.
        (_slow_pattern_policy(), "git " + "reset x " * 5000 + "--hard", True),
        (_slow_pattern_policy(), "git " + "reset x " * 5000, False),
    ],
)
def test_enforcer_decision_table(policy: dict[str, object], command: str, denied: bool) -> None:
    assert (_hook.decide(policy, command) is not None) is denied
