"""Spawn operation input/output models and shared lightweight helpers."""

import shlex
from typing import NotRequired, TypedDict

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_serializer

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.spawn_lifecycle import is_active_spawn_status, is_terminal_spawn_status
from meridian.lib.core.util import FormatContext
from meridian.lib.launch.request import SessionRequest


def _empty_template_vars() -> dict[str, str]:
    return {}


def normalize_goal(goal: str | None) -> str | None:
    if goal is None:
        return None
    normalized = goal.strip()
    if not normalized:
        raise ValueError("--goal cannot be empty")
    return normalized


def build_goal_contract_preview(goal: str | None) -> str | None:
    normalized_goal = normalize_goal(goal)
    if normalized_goal is None:
        return None
    from meridian.lib.launch.prompt import build_goal_instruction

    return build_goal_instruction(normalized_goal)


def _truncate_cell(value: str, *, max_chars: int) -> str:
    compact = " ".join(value.split()).strip()
    if len(compact) <= max_chars:
        return compact
    return f"{compact[: max_chars - 3].rstrip()}..."


def _background_wait_note(spawn_id: str) -> str:
    return (
        f"Background spawn submitted.\n"
        f"Spawn id: {spawn_id}\n"
        f"\n"
        f"After spawning all subagents, you MUST run:\n"
        f"\n"
        f"  meridian spawn wait\n"
        f"\n"
        f"This waits for all pending spawns for this chat.\n"
        f"Or wait for this spawn only: meridian spawn wait {spawn_id}"
    )


class SpawnLaunchOptionUpdates(TypedDict):
    dry_run: bool
    verbose: bool
    quiet: bool
    stream: bool
    background: bool
    project_root: str | None
    timeout: float | None
    resident_rearm_budget: int | None
    approval: str | None
    autocompact: int | None
    autocompact_pct: NotRequired[int | None]
    effort: str | None
    sandbox: str | None
    harness: str | None
    passthrough_args: tuple[str, ...]
    debug: bool
    task_ping_interval: str | None
    task_ping_reset_on_activity: bool | None
    env: tuple[str, ...]


class SpawnLaunchOptions(BaseModel):
    model_config = ConfigDict(frozen=True)

    dry_run: bool = False
    verbose: bool = False
    quiet: bool = False
    stream: bool = False
    background: bool = False
    project_root: str | None = None
    timeout: float | None = None
    resident_rearm_budget: int | None = None
    approval: str | None = None
    autocompact: int | None = None
    autocompact_pct: int | None = None
    effort: str | None = None
    sandbox: str | None = None
    harness: str | None = None
    passthrough_args: tuple[str, ...] = ()
    debug: bool = False
    task_ping_interval: str | None = None
    task_ping_reset_on_activity: bool | None = None
    env: tuple[str, ...] = ()

    def launch_option_updates(self) -> SpawnLaunchOptionUpdates:
        return {
            "dry_run": self.dry_run,
            "verbose": self.verbose,
            "quiet": self.quiet,
            "stream": self.stream,
            "background": self.background,
            "project_root": self.project_root,
            "timeout": self.timeout,
            "resident_rearm_budget": self.resident_rearm_budget,
            "approval": self.approval,
            "autocompact": self.autocompact,
            "autocompact_pct": self.autocompact_pct,
            "effort": self.effort,
            "sandbox": self.sandbox,
            "harness": self.harness,
            "passthrough_args": self.passthrough_args,
            "debug": self.debug,
            "task_ping_interval": self.task_ping_interval,
            "task_ping_reset_on_activity": self.task_ping_reset_on_activity,
            "env": self.env,
        }


class SpawnCreateInput(SpawnLaunchOptions):
    prompt: str = ""
    goal: str | None = None
    model: str | None = None
    files: tuple[str, ...] = ()
    context_from: tuple[str, ...] = ()
    template_vars: tuple[str, ...] = ()
    agent: str | None = None
    agent_opt_out: bool = False
    skills: tuple[str, ...] = ()
    desc: str = ""
    work: str = ""
    task_dir: str | None = None
    caller_cwd: str | None = None
    session: SessionRequest = SessionRequest()
    launch_policy_snapshot: LaunchPolicySnapshot | None = None

    @field_validator("goal", mode="before")
    @classmethod
    def _normalize_goal(cls, value: object) -> object:
        if value is None or not isinstance(value, str):
            return value
        return normalize_goal(value)


class SpawnActionOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    command: str
    status: str
    spawn_id: str | None = None
    message: str | None = None
    error: str | None = None
    current_depth: int | None = None
    max_depth: int | None = None
    model: str | None = None
    harness_id: str | None = None
    warning: str | None = None
    agent: str | None = None
    agent_path: str | None = None
    skills: tuple[str, ...] = ()
    skill_paths: tuple[str, ...] = ()
    reference_files: tuple[str, ...] = ()
    template_vars: dict[str, str] = Field(default_factory=_empty_template_vars)
    context_from_resolved: tuple[str, ...] = ()
    report: str | None = None
    composed_prompt: str | None = None
    goal: str | None = None
    model_selection_requested_token: str | None = None
    model_selection_canonical_id: str | None = None
    model_selection_harness_provenance: str | None = None
    matched_policy_rule: str | None = None
    fallback_chain: tuple[dict[str, object], ...] = ()
    terminal_surface_mode: str | None = None
    project_root: str | None = None
    project_root_source: str | None = None
    runtime_root: str | None = None
    runtime_root_source: str | None = None
    authority_root: str | None = None
    task_cwd: str | None = None
    reference_anchor: str | None = None
    task_cwd_source: str | None = None
    task_cwd_work_item: str | None = None
    cli_command: tuple[str, ...] = ()
    exit_code: int | None = None
    duration_secs: float | None = None
    background: bool = False
    forked_from: str | None = None
    session_log_available: bool | None = None

    @computed_field
    @property
    def goal_contract_preview(self) -> str | None:
        return build_goal_contract_preview(self.goal)

    def _has_distinct_task_cwd(self) -> bool:
        if self.authority_root is None or self.task_cwd is None:
            return False
        return self.task_cwd != self.authority_root

    def _transcript_command(self) -> str | None:
        if self.command != "spawn.create" or self.spawn_id is None or self.background:
            return None
        if self.session_log_available is False:
            return None
        if self.session_log_available is True:
            return f"meridian session log {self.spawn_id}"
        if self.status == "failed":
            return None
        return f"meridian session log {self.spawn_id}"

    def to_wire(self) -> dict[str, object]:
        """Project minimal external JSON shape. Omit nulls and input echo."""
        wire: dict[str, object] = {"status": self.status}
        if self.spawn_id is not None:
            wire["spawn_id"] = self.spawn_id
            if self.background and self.status == "running":
                wire["note"] = _background_wait_note(self.spawn_id)
                wire["terminal"] = False
                wire["wait_required"] = True
                wire["wait_command"] = "meridian spawn wait"
        if self.forked_from is not None:
            wire["forked_from"] = self.forked_from
        if self.duration_secs is not None:
            wire["duration_secs"] = round(self.duration_secs, 2)
        if self.report is not None:
            wire["report"] = self.report
        if self.error is not None:
            wire["error"] = self.error
        if self.status == "failed" and self.spawn_id is None:
            if self.message is not None:
                wire["message"] = self.message
            if self.model is not None:
                wire["model"] = self.model
            if self.harness_id is not None:
                wire["harness_id"] = self.harness_id
        if self.warning is not None:
            wire["warning"] = self.warning
        if self.exit_code is not None:
            wire["exit_code"] = self.exit_code
        if self.context_from_resolved:
            wire["context_from_resolved"] = list(self.context_from_resolved)
        include_task_context = (
            self.status == "dry-run"
            or (self.status == "failed" and self.spawn_id is None)
            or self._has_distinct_task_cwd()
        )
        if include_task_context:
            if self.task_cwd is not None:
                wire["task_cwd"] = self.task_cwd
            if self.task_cwd_source is not None:
                wire["task_cwd_source"] = self.task_cwd_source
            if self.task_cwd_work_item is not None:
                wire["task_cwd_work_item"] = self.task_cwd_work_item
            if self.reference_anchor is not None:
                wire["reference_anchor"] = self.reference_anchor
        transcript_command = self._transcript_command()
        if transcript_command is not None:
            wire["transcript_command"] = transcript_command
        if self.status == "dry-run":
            if self.project_root is not None or self.runtime_root is not None:
                wire["resolved_authority"] = {
                    key: value
                    for key, value in {
                        "project_root": self.project_root,
                        "project_root_source": self.project_root_source,
                        "runtime_root": self.runtime_root,
                        "runtime_root_source": self.runtime_root_source,
                    }.items()
                    if value is not None
                }
            if self.model is not None:
                wire["model"] = self.model
            if self.harness_id is not None:
                wire["harness_id"] = self.harness_id
            if self.agent is not None:
                wire["agent"] = self.agent
            if self.agent_path is not None:
                wire["agent_path"] = self.agent_path
            if self.skills:
                wire["skills"] = list(self.skills)
            if self.skill_paths:
                wire["skill_paths"] = list(self.skill_paths)
            if self.reference_files:
                wire["reference_files"] = list(self.reference_files)
            if self.template_vars:
                wire["template_vars"] = dict(self.template_vars)
            if self.composed_prompt is not None:
                wire["composed_prompt"] = self.composed_prompt
            if self.goal is not None:
                wire["goal"] = self.goal
            goal_contract_preview = self.goal_contract_preview
            if goal_contract_preview is not None:
                wire["goal_contract_preview"] = goal_contract_preview
            if self.model_selection_requested_token is not None:
                wire["model_selection"] = {
                    "requested_token": self.model_selection_requested_token,
                    "canonical_model_id": self.model_selection_canonical_id,
                    "harness_provenance": self.model_selection_harness_provenance,
                }
            if self.matched_policy_rule is not None:
                wire["matched_policy_rule"] = self.matched_policy_rule
            if self.fallback_chain:
                wire["fallback_chain"] = list(self.fallback_chain)
            if self.terminal_surface_mode is not None:
                wire["terminal_surface_mode"] = self.terminal_surface_mode
            if self.cli_command:
                wire["cli_command"] = list(self.cli_command)
        return wire

    def to_agent_wire(self) -> dict[str, object]:
        """Project sparse JSON for implicit agent-mode consumers."""
        if not (self.background and self.status == "running"):
            return self.to_wire()

        wire: dict[str, object] = {"status": self.status}
        if self.spawn_id is not None:
            wire["spawn_id"] = self.spawn_id
            wire["note"] = _background_wait_note(self.spawn_id)
            wire["terminal"] = False
            wire["wait_required"] = True
            wire["wait_command"] = "meridian spawn wait"
        if self.warning is not None:
            wire["warning"] = self.warning
        if self._has_distinct_task_cwd() and self.task_cwd is not None:
            wire["task_cwd"] = self.task_cwd
            if self.task_cwd_source is not None:
                wire["task_cwd_source"] = self.task_cwd_source
            if self.task_cwd_work_item is not None:
                wire["task_cwd_work_item"] = self.task_cwd_work_item
            if self.reference_anchor is not None:
                wire["reference_anchor"] = self.reference_anchor
        return wire

    def to_cli_output(
        self,
        *,
        format: str,
        explicit_format: str | None,
        agent_mode: bool,
    ) -> object:
        """CLI output protocol: shape output for the given format context."""
        _ = agent_mode
        if format != "json":
            return self
        if explicit_format is None:
            return self.to_agent_wire()
        return self.to_wire()

    def format_text(self, ctx: FormatContext | None = None) -> str:
        effective_ctx = ctx or FormatContext()
        if (
            self.command == "spawn.create"
            and is_terminal_spawn_status(self.status)
            and not self.background
            and effective_ctx.verbosity == 0
        ):
            return self._format_terminal_text()
        return self._format_default_text()

    def _format_terminal_text(self) -> str:
        if self.spawn_id is None:
            return self._format_default_text()
        duration_suffix = ""
        if self.duration_secs is not None:
            duration_suffix = f" ({self.duration_secs:.1f}s)"
        lines = [f"{self.spawn_id} {self.status}{duration_suffix}"]
        if self.warning:
            lines.append(f"Warning: {self.warning}")
        if self.error:
            lines.append(f"Error: {self.error}")
        if self._has_distinct_task_cwd():
            task_line = f"Spawn is instructed to implement in this task dir: {self.task_cwd}"
            attribution: list[str] = []
            if self.task_cwd_work_item:
                attribution.append(f"work: {self.task_cwd_work_item}")
            if self.task_cwd_source:
                attribution.append(self.task_cwd_source)
            if attribution:
                task_line += "  (" + ", ".join(attribution) + ")"
            lines.append(task_line)
            if self.reference_anchor:
                lines.append(f"Reference anchor: {self.reference_anchor}")
        report_text = (self.report or "").strip()
        if report_text:
            lines.append("")
            lines.append(report_text)
        elif self.status == "succeeded":
            lines.append("")
            lines.append("(no report)")
        transcript_command = self._transcript_command()
        if transcript_command is not None:
            lines.append("")
            lines.append(f"Transcript: {transcript_command}")
        return "\n".join(lines)

    def _format_default_text(self) -> str:
        if (
            self.background
            and self.status == "running"
            and self.spawn_id
            and not self.message
        ):
            text = _background_wait_note(self.spawn_id)
            if self.warning:
                text = f"{text}\nWarning: {self.warning}"
            return text

        lines: list[str] = []
        if self.message:
            lines.append(self.message)
        else:
            lines.append(f"Spawn {self.status}.")
        if self.spawn_id:
            lines.append(f"Spawn id: {self.spawn_id}")
        if self.forked_from:
            lines.append(f"Forked from: {self.forked_from}")
        if self.model and self.harness_id:
            lines.append(f"Model: {self.model} ({self.harness_id})")
        elif self.model:
            lines.append(f"Model: {self.model}")
        if self.status == "dry-run" and self.model_selection_harness_provenance:
            lines.append(f"Routing: {self.model_selection_harness_provenance}")
        if self.status == "dry-run" and self.matched_policy_rule:
            lines.append(f"Matched policy rule: {self.matched_policy_rule}")
        if self.status == "dry-run" and self.project_root:
            lines.append(f"Project root: {self.project_root}")
            if self.project_root_source:
                lines.append(f"Project root source: {self.project_root_source}")
        if self.status == "dry-run" and self.runtime_root:
            lines.append(f"Write root: {self.runtime_root}")
            if self.runtime_root_source:
                lines.append(f"Write root source: {self.runtime_root_source}")
        if self.authority_root and self.task_cwd and self.task_cwd != self.authority_root:
            task_line = f"Spawn is instructed to implement in this task dir: {self.task_cwd}"
            attribution: list[str] = []
            if self.task_cwd_work_item:
                attribution.append(f"work: {self.task_cwd_work_item}")
            if self.task_cwd_source:
                attribution.append(self.task_cwd_source)
            if attribution:
                task_line += "  (" + ", ".join(attribution) + ")"
            lines.append(task_line)
            if self.reference_anchor:
                lines.append(f"Reference anchor: {self.reference_anchor}")
        elif self.authority_root:
            lines.append(f"CWD: {self.authority_root}")
        if self.status == "dry-run" and self.goal is not None:
            lines.append(f"Goal: {self.goal}")
            goal_contract_preview = self.goal_contract_preview
            if goal_contract_preview is not None:
                lines.append("")
                lines.append("Completion contract preview:")
                lines.append("")
                lines.append(goal_contract_preview)
        if self.error:
            lines.append(f"Error: {self.error}")
        if self.warning:
            lines.append(f"Warning: {self.warning}")
        if self.cli_command:
            lines.append(shlex.join(self.cli_command))
        if self.exit_code is not None:
            lines.append(f"Exit code: {self.exit_code}")
        transcript_command = self._transcript_command()
        if transcript_command is not None:
            report_text = (self.report or "").strip()
            if report_text:
                lines.append("")
                lines.append(report_text)
            lines.append(f"Transcript: {transcript_command}")
        return "\n".join(lines)


class SpawnListInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: SpawnStatus | None = None
    statuses: tuple[SpawnStatus, ...] | None = None
    model: str | None = None
    profile: str | None = None
    primary: bool = False
    limit: int = 20
    failed: bool = False
    project_root: str | None = None


class SpawnChildrenInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    spawn_id: str
    project_root: str | None = None


class SpawnStatsInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    spawn_id: str | None = None
    session: str | None = None
    flat: bool = False
    project_root: str | None = None


class SpawnStatsChild(BaseModel):
    model_config = ConfigDict(frozen=True)

    spawn_id: str
    status: str
    model: str
    duration_secs: float | None
    cost_usd: float | None
    input_tokens: int | None
    output_tokens: int | None

    def as_row(self) -> list[str]:
        return [
            self.spawn_id,
            self.status,
            _truncate_cell(self.model, max_chars=18),
            f"{self.duration_secs:.1f}s" if self.duration_secs is not None else "-",
            f"${self.cost_usd:.4f}" if self.cost_usd is not None else "-",
        ]


class ModelStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    total: int = 0
    succeeded: int = 0
    failed: int = 0
    cancelled: int = 0
    timed_out: int = 0
    running: int = 0
    finalizing: int = 0
    cost_usd: float = 0.0

    def success_rate(self) -> str:
        finished = self.succeeded + self.failed + self.cancelled + self.timed_out
        if finished == 0:
            return "-"
        return f"{self.succeeded / finished * 100:.0f}%"


class SpawnStatsOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    total_runs: int
    succeeded: int
    failed: int
    cancelled: int
    timed_out: int
    running: int
    finalizing: int = 0
    total_duration_secs: float
    total_cost_usd: float
    models: dict[str, ModelStats]
    children: tuple[SpawnStatsChild, ...] = ()

    def _pct(self, n: int) -> str:
        if self.total_runs == 0:
            return "0.0%"
        return f"{n / self.total_runs * 100:.1f}%"

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        lines = [
            f"total_runs: {self.total_runs}",
            f"succeeded: {self.succeeded} ({self._pct(self.succeeded)})",
            f"failed: {self.failed} ({self._pct(self.failed)})",
            f"cancelled: {self.cancelled} ({self._pct(self.cancelled)})",
            f"timed_out: {self.timed_out} ({self._pct(self.timed_out)})",
            f"running: {self.running}",
            f"finalizing: {self.finalizing}",
            f"total_duration: {self.total_duration_secs:.1f}s",
            f"total_cost: ${self.total_cost_usd:.4f}",
        ]
        if self.models:
            from meridian.lib.core.formatting import tabular

            lines.append("")
            rows = [["model", "total", "succeeded", "failed", "timed_out", "success%", "cost"]]
            for model, stats in self.models.items():
                label = model if model else "(unknown)"
                rows.append(
                    [
                        label,
                        str(stats.total),
                        str(stats.succeeded),
                        str(stats.failed),
                        str(stats.timed_out),
                        stats.success_rate(),
                        f"${stats.cost_usd:.2f}" if stats.cost_usd else "-",
                    ]
                )
            lines.append(tabular(rows))
        if self.children:
            from meridian.lib.core.formatting import tabular

            lines.append("")
            rows = [["spawn", "status", "model", "duration", "cost"]]
            rows.extend(child.as_row() for child in self.children)
            lines.append(tabular(rows))
        return "\n".join(lines)


class SpawnListEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    spawn_id: str
    status: str
    status_display: str | None = None
    model: str
    agent: str | None = None
    desc: str | None = None
    kind: str | None = None
    activity: str | None = None
    managed_backend: bool = False
    duration_secs: float | None
    cost_usd: float | None

    def display_status(self) -> str:
        shown = (self.status_display or "").strip()
        if shown:
            return shown
        return self.status

    def as_row(self) -> list[str]:
        """Return columnar cells for tabular alignment."""
        return [
            self.spawn_id,
            self.display_status(),
            _truncate_cell(self.model, max_chars=18),
            f"{self.duration_secs:.1f}s" if self.duration_secs is not None else "-",
        ]


class SpawnListOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    spawns: tuple[SpawnListEntry, ...]
    total_count: int | None = None
    truncated: bool = False
    text_view: str = "list"

    @model_serializer(mode="plain")
    def _serialize(self) -> dict[str, object]:
        serialized_spawns: list[dict[str, object]] = []
        for entry in self.spawns:
            payload_entry: dict[str, object] = {
                "spawn_id": entry.spawn_id,
                "status": entry.status,
                "agent": entry.agent,
                "desc": entry.desc,
                "model": entry.model,
                "duration_secs": entry.duration_secs,
                "cost_usd": entry.cost_usd,
            }
            if entry.kind is not None:
                payload_entry["kind"] = entry.kind
            if entry.activity is not None:
                payload_entry["activity"] = entry.activity
            if entry.managed_backend:
                payload_entry["managed_backend"] = True
            serialized_spawns.append(payload_entry)

        payload: dict[str, object] = {
            "spawns": serialized_spawns,
            "truncated": self.truncated,
        }
        if self.total_count is not None:
            payload["total_count"] = self.total_count
        return payload

    def format_text(self, ctx: FormatContext | None = None) -> str:
        """Columnar list of spawns for text output mode."""
        if not self.spawns:
            if self.text_view == "children":
                return "(no children)"
            return "(no spawns)"
        from meridian.lib.core.formatting import tabular

        if self.text_view == "children":
            rows = [["spawn", "status", "agent", "desc", "model", "duration"]]
            for entry in self.spawns:
                rows.append(
                    [
                        entry.spawn_id,
                        entry.display_status(),
                        entry.agent or "-",
                        entry.desc or "-",
                        _truncate_cell(entry.model, max_chars=18) if entry.model else "-",
                        f"{entry.duration_secs:.1f}s" if entry.duration_secs is not None else "-",
                    ]
                )
        else:
            rows = [["spawn", "status", "model", "duration"]]
            rows.extend(entry.as_row() for entry in self.spawns)
        result = tabular(rows)
        if self.truncated and self.total_count is not None:
            result += (
                f"\n({len(self.spawns)} of {self.total_count} shown — use --limit to see more)"
            )
        return result


class SpawnShowInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    spawn_id: str
    include_report_body: bool = True
    project_root: str | None = None


class SpawnSignalInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    spawn_id: str | None = None
    project_root: str | None = None

class SpawnStatusInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    spawn_id: str
    include_report_body: bool = False
    project_root: str | None = None


class SpawnCancelInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    spawn_id: str
    project_root: str | None = None


class SpawnCancelAllInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    work: str | None = None
    project_root: str | None = None
    include_primaries: bool = False
    include_others: bool = False


class SpawnCancelAllOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    work: str | None = None
    total_running: int
    cancelled_count: int
    finalizing_count: int = 0
    failed_count: int = 0
    timed_out_count: int = 0
    results: tuple["SpawnActionOutput", ...] = ()

    def format_text(self, ctx: object | None = None) -> str:
        _ = ctx
        scope = f" for work {self.work}" if self.work else ""
        if self.total_running == 0:
            return f"No running spawns to cancel{scope}."

        lines = [f"Requested cancellation for {self.cancelled_count} running spawn(s){scope}."]
        if self.finalizing_count:
            lines.append(f"{self.finalizing_count} cancellation(s) still finalizing.")
        if self.failed_count:
            lines.append(f"{self.failed_count} cancellation(s) failed.")
            for result in self.results:
                if result.status == "failed":
                    lines.append(result.format_text())
        if self.timed_out_count:
            lines.append(f"{self.timed_out_count} cancellation(s) timed out.")
        return "\n".join(lines)


class SpawnDetailOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    spawn_id: str
    status: str
    model: str
    harness: str
    kind: str | None = None
    activity: str | None = None
    managed_backend: bool = False
    backend_pid: int | None = None
    tui_pid: int | None = None
    backend_port: int | None = None
    parent_id: str | None = None
    work_id: str | None = None
    authority_root: str | None = None
    task_cwd: str | None = None
    goal: str | None = None
    desc: str | None = None
    started_at: str
    finished_at: str | None
    duration_secs: float | None
    exit_code: int | None
    failure_reason: str | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_usd: float | None
    cost_is_estimate: bool = False
    report_path: str | None
    report_summary: str | None
    report_body: str | None
    harness_session_id: str | None = None
    pi_lifecycle_phase: str | None = None
    pi_cleanup_status: str | None = None
    pi_cleanup_phase: str | None = None
    pi_cleanup_reason: str | None = None
    pi_cleanup_error: str | None = None
    last_message: str | None = None
    log_path: str | None = None
    last_attempt_exited_at: str | None = None
    last_attempt_exit_code: int | None = None
    session_config_dir: str | None = None
    session_log_available: bool = False

    def _normalized_report_body(self) -> str | None:
        report_text = (self.report_body or "").strip()
        if not report_text:
            return None
        return report_text

    def _status_for_compact_or_moderate(self) -> str:
        status_str = self.status
        if self.status == "finalizing":
            return "finalizing (cleanup in progress)"
        if self.exit_code is not None and self.status == "failed":
            status_str += f" (exit {self.exit_code})"
        return status_str

    def _failure_line_for_compact_or_moderate(self) -> tuple[str, str] | None:
        failure_value = self.failure_reason
        if failure_value is None:
            return None
        if failure_value == "orphan_finalization":
            failure_value = (
                "orphan_finalization (harness likely completed; "
                "report.md may still contain useful content)"
            )
        label = "Warning" if self.status == "succeeded" else "Failure"
        return label, failure_value

    def _report_suffix(self) -> str:
        report_text = self._normalized_report_body()
        if report_text is None:
            return ""
        return f"\n\n{report_text}"

    def report_table_value(self) -> str:
        return self.report_path or "-"

    def report_section(self) -> str | None:
        report_text = self._normalized_report_body()
        if report_text is None:
            return None
        return f"Report for {self.spawn_id}\n{report_text}"

    def transcript_command(self) -> str | None:
        if not self.session_log_available:
            return None
        return f"meridian session log {self.spawn_id}"

    def _has_distinct_task_cwd(self) -> bool:
        if self.authority_root is None or self.task_cwd is None:
            return False
        return self.task_cwd != self.authority_root

    def _task_dir_line(self) -> str:
        task_line = f"Spawn is instructed to implement in this task dir: {self.task_cwd}"
        work_value = (self.work_id or "").strip()
        if work_value:
            task_line += f"  (work: {work_value})"
        return task_line

    def to_cli_wire(self) -> dict[str, object]:
        """Project slim JSON shape for wire serialization. Omits internal fields."""
        wire: dict[str, object] = {
            "spawn_id": self.spawn_id,
            "status": self.status,
            "model": self.model,
            "harness": self.harness,
        }
        if self.kind is not None:
            wire["kind"] = self.kind
        if self.activity is not None:
            wire["activity"] = self.activity
        if self.managed_backend:
            wire["managed_backend"] = True
        if self.backend_pid is not None:
            wire["backend_pid"] = self.backend_pid
        if self.tui_pid is not None:
            wire["tui_pid"] = self.tui_pid
        if self.backend_port is not None:
            wire["backend_port"] = self.backend_port
        if self.kind == "primary" and self.harness_session_id is not None:
            wire["harness_session_id"] = self.harness_session_id
        if self.pi_lifecycle_phase is not None:
            wire["pi_lifecycle_phase"] = self.pi_lifecycle_phase
        if self.pi_cleanup_status is not None:
            wire["pi_cleanup_status"] = self.pi_cleanup_status
        if self.pi_cleanup_phase is not None:
            wire["pi_cleanup_phase"] = self.pi_cleanup_phase
        if self.pi_cleanup_reason is not None:
            wire["pi_cleanup_reason"] = self.pi_cleanup_reason
        if self.pi_cleanup_error is not None:
            wire["pi_cleanup_error"] = self.pi_cleanup_error
        if self.parent_id is not None:
            wire["parent_id"] = self.parent_id
        if self.work_id is not None:
            wire["work_id"] = self.work_id
        if self._has_distinct_task_cwd() and self.task_cwd is not None:
            wire["task_cwd"] = self.task_cwd
        if self.desc is not None:
            wire["desc"] = self.desc
        if self.goal is not None:
            wire["goal"] = self.goal
        if self.duration_secs is not None:
            wire["duration_secs"] = round(self.duration_secs, 2)
        if self.exit_code is not None:
            wire["exit_code"] = self.exit_code
        if self.failure_reason is not None:
            wire["failure_reason"] = self.failure_reason
        if self.cost_usd is not None:
            wire["cost_usd"] = round(self.cost_usd, 4)
            if self.cost_is_estimate:
                wire["cost_estimated"] = True
        if self.input_tokens is not None:
            wire["input_tokens"] = self.input_tokens
        if self.output_tokens is not None:
            wire["output_tokens"] = self.output_tokens
        if self.cache_read_input_tokens is not None:
            wire["cache_read_input_tokens"] = self.cache_read_input_tokens
        if self.cache_creation_input_tokens is not None:
            wire["cache_creation_input_tokens"] = self.cache_creation_input_tokens
        if self.reasoning_tokens is not None:
            wire["reasoning_tokens"] = self.reasoning_tokens
        if self.report_path is not None:
            wire["report_path"] = self.report_path
        if self.report_summary is not None:
            wire["report_summary"] = self.report_summary
        if self.report_body is not None:
            wire["report_body"] = self.report_body
        if self.session_config_dir is not None:
            wire["session_config_dir"] = self.session_config_dir
        return wire

    def to_cli_output(
        self,
        *,
        format: str,
        explicit_format: str | None,
        agent_mode: bool,
    ) -> object:
        """CLI output protocol: shape output for the given format context."""
        _ = explicit_format
        _ = agent_mode
        if format != "json":
            return self
        return self.to_cli_wire()

    def format_text(self, ctx: FormatContext | None = None) -> str:
        effective_ctx = ctx or FormatContext()
        if effective_ctx.verbosity > 0:
            return self._format_verbose_text(always_show_transcript=True)
        return self._format_moderate_text()

    def format_wait_text(self, ctx: FormatContext | None = None) -> str:
        effective_ctx = ctx or FormatContext()
        if effective_ctx.verbosity > 0:
            return self._format_verbose_text(always_show_transcript=True)
        return self._format_compact_text()

    def _format_compact_text(self) -> str:
        status_str = self._status_for_compact_or_moderate()

        meta_parts: list[str] = []
        if self.duration_secs is not None:
            meta_parts.append(f"{self.duration_secs:.1f}s")
        meta_suffix = f" ({', '.join(meta_parts)})" if meta_parts else ""

        lines = [f"{self.spawn_id} {status_str}{meta_suffix}"]
        failure_line = self._failure_line_for_compact_or_moderate()
        if failure_line is not None:
            label, value = failure_line
            lines.append(f"{label}: {value}")
        if self._has_distinct_task_cwd():
            lines.append(self._task_dir_line())

        report_text = self._normalized_report_body()
        if report_text is not None:
            lines.append("")
            lines.append(report_text)
            lines.append("")
        else:
            lines.append("")
        transcript_command = self.transcript_command()
        if transcript_command is not None:
            lines.append(f"Transcript: {transcript_command}")

        return "\n".join(lines)

    def _format_moderate_text(self) -> str:
        status_str = self._status_for_compact_or_moderate()

        lines = [f"Spawn: {self.spawn_id}"]
        lines.append(f"Status: {status_str}")
        lines.append(f"Model: {self.model} ({self.harness})")
        if self.duration_secs is not None:
            lines.append(f"Duration: {self.duration_secs:.1f}s")
        work_value = (self.work_id or "").strip()
        if work_value:
            lines.append(f"Work: {work_value}")
        if self._has_distinct_task_cwd():
            lines.append(self._task_dir_line())
        if self.report_path is not None:
            lines.append(f"Report: {self.report_path}")
        failure_line = self._failure_line_for_compact_or_moderate()
        if failure_line is not None:
            label, value = failure_line
            lines.append(f"{label}: {value}")

        report_text = self._normalized_report_body()
        if report_text is not None:
            lines.append("")
            lines.append(report_text)

        lines.append("")
        transcript_command = self.transcript_command()
        if transcript_command is not None:
            lines.append(f"Transcript: {transcript_command}")
        return "\n".join(lines)

    def _format_verbose_text(self, *, always_show_transcript: bool = False) -> str:
        """Key-value detail view for text output mode. Omits None/empty fields."""
        from meridian.lib.core.formatting import kv_block

        status_str = self.status
        if self.status == "finalizing":
            status_str = "finalizing (cleanup in progress)"
        elif self.exit_code is not None:
            status_str += f" (exit {self.exit_code})"

        duration_value: str | None = (
            None if self.duration_secs is None else f"{self.duration_secs:.1f}s"
        )

        cost_value: str | None = None if self.cost_usd is None else f"${self.cost_usd:.4f}"
        if cost_value is not None and self.cost_is_estimate:
            cost_value = f"~{cost_value}"

        failure_label: str | None = None
        failure_value = self.failure_reason
        if failure_value is not None:
            failure_label = "Warning" if self.status == "succeeded" else "Failure"
            if failure_value == "orphan_finalization":
                failure_value = (
                    "orphan_finalization (harness likely completed; "
                    "report.md may still contain useful content)"
                )

        work_value = (self.work_id or "").strip() or None
        desc_value = (self.desc or "").strip() or None
        kind_value = (self.kind or "").strip() or None
        activity_value = (self.activity or "").strip() or None
        managed_backend_value: str | None = "true" if self.managed_backend else None
        harness_session_value: str | None = None
        if self.kind == "primary":
            harness_session_value = (self.harness_session_id or "").strip() or None

        parent_value = (self.parent_id or "").strip() or None
        active_status = is_active_spawn_status(self.status)

        pairs: list[tuple[str, str | None]] = [
            ("Spawn", self.spawn_id),
            ("Status", status_str),
            ("Kind", kind_value),
            ("Activity", activity_value),
            ("Managed backend", managed_backend_value),
            ("Backend pid", None if self.backend_pid is None else str(self.backend_pid)),
            ("TUI pid", None if self.tui_pid is None else str(self.tui_pid)),
            ("Backend port", None if self.backend_port is None else str(self.backend_port)),
            ("Harness session", harness_session_value),
            ("Pi phase", self.pi_lifecycle_phase),
            ("Pi cleanup status", self.pi_cleanup_status),
            ("Pi cleanup phase", self.pi_cleanup_phase),
            ("Pi cleanup reason", self.pi_cleanup_reason),
            ("Pi cleanup error", self.pi_cleanup_error),
            ("Last attempt exited at", None if active_status else self.last_attempt_exited_at),
            (
                "Last attempt exit code",
                None
                if (active_status or self.last_attempt_exit_code is None)
                else str(self.last_attempt_exit_code),
            ),
            ("Model", f"{self.model} ({self.harness})"),
            ("Duration", duration_value),
            ("Parent", parent_value),
            ("Work", work_value),
            (
                "Spawn is instructed to implement in this task dir",
                self.task_cwd if self._has_distinct_task_cwd() else None,
            ),
            ("Goal", self.goal),
            ("Desc", desc_value),
            (failure_label or "Failure", failure_value),
            ("Input tokens", None if self.input_tokens is None else str(self.input_tokens)),
            ("Output tokens", None if self.output_tokens is None else str(self.output_tokens)),
            (
                "Cache read tokens",
                None if self.cache_read_input_tokens is None else str(self.cache_read_input_tokens),
            ),
            (
                "Cache creation tokens",
                None
                if self.cache_creation_input_tokens is None
                else str(self.cache_creation_input_tokens),
            ),
            (
                "Reasoning tokens",
                None if self.reasoning_tokens is None else str(self.reasoning_tokens),
            ),
            ("Cost", cost_value),
            ("Report", self.report_path),
            ("Last message", self.last_message),
            (
                "Progress",
                f"meridian session log {self.spawn_id}" if self.log_path else None,
            ),
            (
                "Transcript",
                self.transcript_command()
                if always_show_transcript or self.session_log_available
                else None,
            ),
        ]
        return kv_block(pairs) + self._report_suffix()


class SpawnWrittenFilesInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    spawn_id: str
    project_root: str | None = None


class SpawnWrittenFilesOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    spawn_id: str
    written_files: tuple[str, ...]

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        if not self.written_files:
            return ""
        return "\n".join(self.written_files)


class SpawnSubagentsInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    project_root: str | None = None


class SpawnSubagentsOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    names: tuple[str, ...]

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        return "\n".join(self.names)


class SpawnContinueInput(SpawnLaunchOptions):
    spawn_id: str
    prompt: str
    model: str = ""
    files: tuple[str, ...] = ()
    template_vars: tuple[str, ...] = ()
    agent: str | None = None
    agent_opt_out: bool = False
    skills: tuple[str, ...] = ()
    goal: str | None = None
    desc: str = ""
    work: str = ""
    task_dir: str | None = None
    caller_cwd: str | None = None
    fork: bool = False

    @field_validator("goal", mode="before")
    @classmethod
    def _normalize_goal(cls, value: object) -> object:
        if value is None or not isinstance(value, str):
            return value
        return normalize_goal(value)


class SpawnForkInput(SpawnLaunchOptions):
    source_ref: str
    prompt: str
    model: str = ""
    files: tuple[str, ...] = ()
    template_vars: tuple[str, ...] = ()
    agent: str | None = None
    agent_opt_out: bool = False
    skills: tuple[str, ...] = ()
    inherit_source_skills: bool = False
    goal: str | None = None
    desc: str = ""
    work: str = ""
    task_dir: str | None = None
    caller_cwd: str | None = None

    @field_validator("goal", mode="before")
    @classmethod
    def _normalize_goal(cls, value: object) -> object:
        if value is None or not isinstance(value, str):
            return value
        return normalize_goal(value)


class SpawnWaitInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    spawn_ids: tuple[str, ...] = ()
    # Compatibility alias for MCP clients that still send `spawn_id`.
    spawn_id: str | None = None
    timeout: float | None = None
    yield_after_secs: float | None = None
    timeout_explicit: bool = False
    poll_interval_secs: float | None = None
    fail_fast: bool = False
    verbose: bool = False
    quiet: bool = False
    include_report_body: bool = False
    project_root: str | None = None


class SpawnWaitMultiOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    spawns: tuple[SpawnDetailOutput, ...]
    total_runs: int
    succeeded_runs: int
    failed_runs: int
    cancelled_runs: int
    timed_out_runs: int = 0
    any_failed: bool
    fail_fast: bool = False
    pending_ids: tuple[str, ...] = ()
    checkpoint: bool = False
    checkpoint_pending_ids: tuple[str, ...] = ()
    checkpoint_chat_id: str | None = None
    checkpoint_elapsed_secs: float | None = None
    # Compatibility fields for single-run callers.
    spawn_id: str | None = None
    status: str | None = None
    exit_code: int | None = None

    def format_text(self, ctx: FormatContext | None = None) -> str:
        """Render waited spawns, expanding report content when available."""
        effective_ctx = ctx or FormatContext()
        if self.checkpoint:
            return self._format_checkpoint_text()
        if self.fail_fast:
            failed_ids = ", ".join(
                spawn.spawn_id
                for spawn in self.spawns
                if spawn.status in {"failed", "timed_out"}
            )
            pending_ids = ", ".join(self.pending_ids)
            details = self.model_copy(
                update={"fail_fast": False, "pending_ids": ()}
            ).format_text(effective_ctx)
            summary = (
                f"Fail-fast: {failed_ids} failed or timed out. "
                f"Still pending: {pending_ids}."
            )
            return f"{summary}\n\n{details}" if details else summary
        if not self.spawns:
            return ""
        if len(self.spawns) == 1:
            return self.spawns[0].format_wait_text(effective_ctx)

        from meridian.lib.core.formatting import tabular

        rows = [["spawn_id", "status", "duration", "exit", "report"]]
        rows.extend(
            [
                run.spawn_id,
                run.status,
                f"{run.duration_secs:.1f}s" if run.duration_secs is not None else "-",
                str(run.exit_code) if run.exit_code is not None else "-",
                run.report_table_value(),
            ]
            for run in self.spawns
        )
        table = tabular(rows)

        report_sections: list[str] = []
        for run in self.spawns:
            section = run.report_section()
            if section is not None:
                report_sections.append(section)
        if not report_sections:
            return table
        return f"{table}\n\n" + "\n\n".join(report_sections)

    def _format_checkpoint_text(self) -> str:
        if self.checkpoint_elapsed_secs is None:
            elapsed = ""
        elif self.checkpoint_elapsed_secs >= 60:
            elapsed = f" after {self.checkpoint_elapsed_secs / 60:.0f}m"
        else:
            elapsed = f" after {self.checkpoint_elapsed_secs:.0f}s"
        lines = [f"Wait checkpoint{elapsed}. Still pending:"]
        for sid in self.checkpoint_pending_ids:
            detail = next((spawn for spawn in self.spawns if spawn.spawn_id == sid), None)
            status = detail.status if detail else "unknown"
            desc = (detail.desc or "").strip() if detail else ""
            line = f"  {sid}  {status}"
            if desc:
                line += f"  {desc}"
            lines.append(line)
        lines.append("")
        if self.checkpoint_chat_id:
            lines.append("Run `meridian spawn wait` again to continue.")
        elif self.checkpoint_pending_ids:
            ids_str = " ".join(self.checkpoint_pending_ids)
            lines.append(f"Run `meridian spawn wait {ids_str}` again to continue.")
        else:
            lines.append("Run `meridian spawn wait` again to continue.")
        return "\n".join(lines)

    def to_cli_wire(self) -> dict[str, object]:
        """Sparse wire projection. Includes spawn summaries, not full details."""
        wire: dict[str, object] = {
            "total_runs": self.total_runs,
            "succeeded_runs": self.succeeded_runs,
            "failed_runs": self.failed_runs,
            "cancelled_runs": self.cancelled_runs,
            "timed_out_runs": self.timed_out_runs,
            "any_failed": self.any_failed,
        }
        if self.fail_fast:
            wire["fail_fast"] = True
            wire["pending_ids"] = list(self.pending_ids)
        if self.checkpoint:
            wire["checkpoint"] = True
            wire["checkpoint_pending_ids"] = list(self.checkpoint_pending_ids)
            if self.checkpoint_elapsed_secs is not None:
                wire["checkpoint_elapsed_secs"] = round(self.checkpoint_elapsed_secs, 2)
            if self.checkpoint_chat_id:
                wire["checkpoint_chat_id"] = self.checkpoint_chat_id
        # Compatibility fields for single-run callers
        if self.spawn_id is not None:
            wire["spawn_id"] = self.spawn_id
        if self.status is not None:
            wire["status"] = self.status
        if self.exit_code is not None:
            wire["exit_code"] = self.exit_code
        if len(self.spawns) == 1:
            single = self.spawns[0]
            transcript_command = single.transcript_command()
            if transcript_command is not None:
                wire["transcript_command"] = transcript_command
            if single.report_body is not None:
                wire["report_body"] = single.report_body
        # Sparse spawn details.
        spawn_wires: list[dict[str, object]] = []
        for spawn in self.spawns:
            spawn_wire = spawn.to_cli_wire()
            transcript_command = spawn.transcript_command()
            if transcript_command is not None:
                spawn_wire["transcript_command"] = transcript_command
            spawn_wires.append(spawn_wire)
        wire["spawns"] = spawn_wires
        return wire

    def to_cli_output(
        self,
        *,
        format: str,
        explicit_format: str | None,
        agent_mode: bool,
    ) -> object:
        """CLI output protocol: shape output for the given format context."""
        _ = explicit_format
        _ = agent_mode
        if format != "json":
            return self
        return self.to_cli_wire()


__all__ = [
    "SpawnActionOutput",
    "SpawnCancelInput",
    "SpawnContinueInput",
    "SpawnCreateInput",
    "SpawnDetailOutput",
    "SpawnLaunchOptionUpdates",
    "SpawnLaunchOptions",
    "SpawnListEntry",
    "SpawnListInput",
    "SpawnListOutput",
    "SpawnShowInput",
    "SpawnSignalInput",
    "SpawnStatsInput",
    "SpawnStatsOutput",
    "SpawnStatusInput",
    "SpawnSubagentsInput",
    "SpawnSubagentsOutput",
    "SpawnWaitInput",
    "SpawnWaitMultiOutput",
    "SpawnWrittenFilesInput",
    "SpawnWrittenFilesOutput",
    "build_goal_contract_preview",
    "normalize_goal",
]
