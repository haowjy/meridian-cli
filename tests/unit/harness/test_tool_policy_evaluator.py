"""The one tool-policy evaluator that Pi's extension and Codex's hook both ask."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from meridian.lib.harness.projections.tool_extension_policy import evaluator_argv

_PATH = Path(evaluator_argv()[-1])


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("tool_policy_evaluate_script", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through it
    spec.loader.exec_module(module)
    return module


_evaluate = _load()

_CASES = json.loads((Path(__file__).parents[2] / "fixtures" / "tool_policy_cases.json").read_text())
_HEADLESS = "Needs approval, and a headless spawn has no one to ask. Do not retry it."


def _policy(
    rules: list[dict[str, object]],
    *,
    fail_closed: bool = False,
    grants: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    return {
        "rules": [
            {"capability": r["tool"], "pattern": None, "label": r["tool"], **r} for r in rules
        ],
        "meridian_commands": grants or [],
        "headless_reject_reason": _HEADLESS,
        "bash_fail_closed": fail_closed,
    }


def _action(
    policy: dict[str, object], command: str, tool: str = "bash", cwd: str = "/", **tool_input: str
) -> str | None:
    tool_input = {"command": command} if tool == "bash" and command else tool_input
    decision = _evaluate.decide(policy, tool, tool_input, cwd)
    return None if decision is None else decision.action


def _slow_pattern_policy() -> dict[str, object]:
    rule = {"tool": "bash", "pattern": "git * reset * x * --hard", "action": "deny"}
    return _policy([rule])


@pytest.mark.parametrize(
    ("policy", "command", "denied"),
    [
        *(
            (
                {**_CASES["policies"][case["policy"]], "headless_reject_reason": _HEADLESS},
                case["command"],
                case["expected"] == "deny",
            )
            for case in _CASES["cases"]
        ),
        # A lone `&` splits; under an allowlist it is refused outright.
        (
            _CASES["policies"]["deny_list"] | {"headless_reject_reason": _HEADLESS},
            "sleep 1 & git reset --hard",
            True,
        ),
        # A pattern with many interior stars must not make the match slow.
        (_slow_pattern_policy(), "git " + "reset x " * 5000 + "--hard", True),
        (_slow_pattern_policy(), "git " + "reset x " * 5000, False),
    ],
)
def test_decision_table(policy: dict[str, object], command: str, denied: bool) -> None:
    assert (_action(policy, command) == "deny") is denied


def test_strictest_matching_rule_wins_and_star_only_when_none_matches() -> None:
    rules = [
        {"tool": "bash", "pattern": "rg *", "action": action} for action in ("allow", "ask", "deny")
    ]
    assert _action(_policy(rules), "rg x") == "deny"
    narrower = [
        {"tool": "bash", "pattern": "rg *", "action": "ask"},
        {"tool": "bash", "pattern": "rg src/*", "action": "allow"},
    ]
    assert _action(_policy(narrower), "rg src/a") == "ask"
    star = [{"tool": "*", "action": "deny"}, {"tool": "read", "action": "allow"}]
    assert _action(_policy(star), "", "read", path="a.txt") == "allow"
    assert _action(_policy(star), "", "grep", path="a") == "deny"


def test_ask_carries_the_headless_reason() -> None:
    decision = _evaluate.decide(
        _policy([{"tool": "bash", "action": "ask"}]), "bash", {"command": "echo"}, "/"
    )
    assert (decision.action, decision.detail, decision.reason) == ("ask", "echo", _HEADLESS)


def test_meridian_grants_are_final_but_only_for_a_whole_command() -> None:
    policy = _policy(
        [
            {"tool": "*", "action": "deny"},
            {"tool": "bash", "pattern": "meridian spawn done private *", "action": "deny"},
        ],
        fail_closed=True,
        grants=[
            {"command": "meridian task-dir", "match": "exact"},
            {"command": "meridian spawn done", "match": "prefix"},
        ],
    )
    for command in [
        "meridian task-dir",
        "meridian spawn done p-1",
        "meridian spawn done private p-1",
    ]:
        assert _action(policy, command) == "allow"
    for command in [
        "meridian task-dir set /tmp",
        "meridian spawn donex",
        "meridian spawn done p-1; touch y",
        "meridian spawn done > output",
    ]:
        assert _action(policy, command) == "deny"


def test_open_shell_scoped_denies_are_best_effort() -> None:
    policy = _policy(
        [
            {"tool": "bash", "action": "allow"},
            {"tool": "bash", "pattern": "git revert *", "action": "deny"},
        ]
    )
    for command in ["echo $HOME", "ls > /tmp/out", "for f in *.py; do echo $f; done"]:
        assert _action(policy, command) != "deny"
    assert _action(policy, "git log -1 && git revert HEAD") == "deny"
    assert _action(policy, "", "bash") == "deny"


def test_unreadable_scope_of_a_scoped_tool_is_refused() -> None:
    assert (
        _action(
            _policy([{"tool": "bash", "pattern": "x *", "action": "allow"}], fail_closed=True), ""
        )
        == "deny"
    )
    future = _policy([{"tool": "future", "pattern": "safe/*", "action": "allow"}])
    assert _action(future, "", "future", path="safe/file") == "deny"


def test_path_scopes_resolve_dots_and_symlinks(tmp_path: Path) -> None:
    (tmp_path / "src" / "nested").mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    (tmp_path / "src" / "link").symlink_to(tmp_path / "outside")
    (tmp_path / "src" / "link-file").symlink_to(tmp_path / "outside" / "new-file")
    star = {"tool": "*", "action": "deny"}
    read = _policy([{"tool": "read", "pattern": "src/*", "action": "allow"}, star])
    write = _policy([{"tool": "write", "pattern": "src/*", "action": "allow"}, star])
    cwd = str(tmp_path)

    def read_action(path: str) -> str | None:
        return _action(read, "", "read", cwd, path=path)

    assert read_action("src/nested/file.ts") == "allow"
    assert read_action(str(tmp_path / "src" / "file.ts")) == "allow"
    assert read_action("src/../../outside/secret") == "deny"
    assert read_action("src/link/secret") == "deny"
    assert read_action("src/link/../secret") == "deny"
    assert _action(write, "", "write", cwd, path="src/link-file") == "deny"


def _run(policy: dict[str, object] | None, request: dict[str, object], *flags: str):
    env = {k: v for k, v in os.environ.items() if k != "_MERIDIAN_TOOL_POLICY"}
    if policy is not None:
        env["_MERIDIAN_TOOL_POLICY"] = json.dumps(policy)
    return subprocess.run(
        [sys.executable, "-I", str(_PATH), *flags],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_script_answers_both_callers() -> None:
    deny = _policy([{"tool": "bash", "pattern": "git reset *", "action": "deny", "label": "r"}])
    refused = {"tool_name": "Bash", "tool_input": {"command": "git reset --hard"}}
    allowed = {"tool_name": "Bash", "tool_input": {"command": "git status"}}

    codex = _run(deny, refused)
    assert (codex.returncode, "r = deny" in codex.stderr) == (2, True)
    assert _run(deny, allowed).returncode == 0

    pi = _run(deny, {**refused, "tool_name": "bash"}, "--json")
    assert pi.returncode == 0
    assert json.loads(pi.stdout)["action"] == "deny"
    assert json.loads(_run(deny, allowed, "--json").stdout)["action"] == "allow"


@pytest.mark.parametrize("flags", [(), ("--json",)])
def test_script_refuses_without_a_policy_or_a_readable_request(flags: tuple[str, ...]) -> None:
    request = {"tool_name": "Bash", "tool_input": {"command": "echo"}}
    assert _run(None, request, *flags).returncode == 2
    assert _run(_policy([]), {"tool_name": "Bash"}, *flags).returncode not in {0, None}
