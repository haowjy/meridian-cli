from __future__ import annotations

from meridian.lib.safety.policy import PermissionPolicy, ReportLine, complete_report, rule_element
from meridian.lib.tools import PROJECT_ORIGIN, ToolRule


def test_mars_note_joins_a_line_the_compile_already_marked_partial() -> None:
    rule = ToolRule(
        capability="bash",
        pattern="*--force*",
        action="deny",
        native=("Bash(*--force*)",),
        origin=PROJECT_ORIGIN,
        note="an agent rule may override this project-wide deny",
    )
    compiled = ReportLine(rule_element(rule), "partial", "hook limits")

    report = complete_report(PermissionPolicy(rules=(rule,)), [compiled], harness="codex")

    line = report[0]
    assert line.status == "partial"
    assert line.note == "hook limits; an agent rule may override this project-wide deny"
