"""Script-based post-run guardrails."""

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from meridian.lib.config.settings import MeridianConfig
from meridian.lib.core.types import SpawnId
from meridian.lib.platform import IS_WINDOWS

DEFAULT_GUARDRAIL_TIMEOUT_SECONDS = MeridianConfig().guardrail_timeout_minutes * 60.0


class GuardrailFailure(BaseModel):
    """One failed guardrail execution."""

    model_config = ConfigDict(frozen=True)

    script: str
    exit_code: int
    stderr: str


class GuardrailResult(BaseModel):
    """Aggregate result for a post-run guardrail pass."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    failures: tuple[GuardrailFailure, ...] = ()


def _resolve_guardrail_command(script: Path) -> list[str]:
    script_text = str(script)
    suffix = script.suffix.lower()

    if IS_WINDOWS:
        if suffix in {".cmd", ".bat"}:
            return ["cmd.exe", "/d", "/c", script_text]
        if suffix == ".ps1":
            return [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-File",
                script_text,
            ]
        return [script_text]

    if os.access(script, os.X_OK):
        return [script_text]
    return ["bash", script_text]


def run_guardrails(
    guardrails: tuple[Path, ...],
    *,
    spawn_id: SpawnId,
    cwd: Path,
    env: Mapping[str, str] | None,
    report_path: Path | None,
    output_log_path: Path,
    timeout_seconds: float = DEFAULT_GUARDRAIL_TIMEOUT_SECONDS,
) -> GuardrailResult:
    """Execute post-run guardrail scripts and collect failures."""

    if not guardrails:
        return GuardrailResult(ok=True)

    child_env = os.environ.copy()
    if env is not None:
        child_env.update(env)
    # Guardrails run untrusted repo scripts, so never pass Meridian secrets through.
    for key in tuple(child_env):
        if key.startswith("MERIDIAN_SECRET_"):
            child_env.pop(key, None)

    child_env["_MERIDIAN_GUARDRAIL_RUN_ID"] = str(spawn_id)
    child_env["_MERIDIAN_GUARDRAIL_OUTPUT_LOG"] = output_log_path.as_posix()
    if report_path is not None:
        child_env["_MERIDIAN_GUARDRAIL_REPORT_PATH"] = report_path.as_posix()

    failures: list[GuardrailFailure] = []
    for script in guardrails:
        command = _resolve_guardrail_command(script)

        try:
            try:
                completed = subprocess.run(
                    command,
                    cwd=cwd,
                    env=child_env,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=timeout_seconds,
                )
            except OSError:
                if IS_WINDOWS or command[0] == "bash":
                    raise
                completed = subprocess.run(
                    ["bash", str(script)],
                    cwd=cwd,
                    env=child_env,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=timeout_seconds,
                )
            if completed.returncode != 0:
                failures.append(
                    GuardrailFailure(
                        script=script.as_posix(),
                        exit_code=completed.returncode,
                        stderr=completed.stderr.strip() or completed.stdout.strip(),
                    )
                )
        except subprocess.TimeoutExpired as exc:
            failures.append(
                GuardrailFailure(
                    script=script.as_posix(),
                    exit_code=124,
                    stderr=f"Guardrail timed out after {timeout_seconds:.1f}s: {exc}",
                )
            )
        except OSError as exc:
            failures.append(
                GuardrailFailure(
                    script=script.as_posix(),
                    exit_code=127,
                    stderr=f"Guardrail failed to start: {exc}",
                )
            )

    return GuardrailResult(ok=not failures, failures=tuple(failures))
