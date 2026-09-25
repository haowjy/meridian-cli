from __future__ import annotations

import os
from pathlib import Path

from meridian.lib.core.types import SpawnId
from meridian.lib.safety.guardrails import run_guardrails


def test_guardrails_receive_report_and_chat_not_runner_log(tmp_path: Path) -> None:
    observed = tmp_path / "env.txt"
    script = tmp_path / "guardrail.sh"
    script.write_text(
        "#!/bin/sh\nprintf '%s\\n' \\\n"
        "  \"$_MERIDIAN_GUARDRAIL_RUN_ID\" \\\n"
        "  \"$_MERIDIAN_GUARDRAIL_CHAT_ID\" \\\n"
        "  \"$_MERIDIAN_GUARDRAIL_REPORT\" \\\n"
        "  \"${_MERIDIAN_GUARDRAIL_OUTPUT_LOG-unset}\" > \"$OBSERVED\"\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    report = tmp_path / "report.md"
    report.write_text("done", encoding="utf-8")

    result = run_guardrails(
        (script,),
        spawn_id=SpawnId("p42"),
        cwd=tmp_path,
        env={"OBSERVED": os.fspath(observed)},
        report_path=report,
        chat_id="c42",
    )

    assert result.ok
    assert observed.read_text(encoding="utf-8").splitlines() == [
        "p42",
        "c42",
        os.fspath(report),
        "unset",
    ]
