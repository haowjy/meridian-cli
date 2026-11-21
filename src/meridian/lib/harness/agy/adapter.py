"""The ``agy`` launch adapter, registered with launch ports only.

Meridian never runs ``agy``: its plugin hosts the session (``harness/plugin_hosted.py``), so
there is no extractor, connection or runner. The adapter builds the command Meridian prints
for the person to run and, as an ``AgyReader``, reads the sessions ``agy`` stores.

Facts the projection rests on (``agy`` 1.5.14, probes in the work item): ``--add-dir`` keeps
the working directory and the added directory's skills, hooks and MCP servers load and run;
``--model <tiered slug>`` with ``--effort`` fails, so Mars folds the effort into the slug and
``--effort`` is never passed; a resume without ``--model`` returns to the person's own default
model.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.adapter import (
    BaseHarnessAdapter,
    BootstrapMode,
    HarnessCapabilities,
    HarnessContract,
    SpawnParams,
    WorkspaceRoots,
)
from meridian.lib.harness.agy.state import AGENT_ENV, APPROVAL_ENV
from meridian.lib.harness.agy.transcript import AgyReader
from meridian.lib.harness.bundle import (
    HarnessProjectionPorts,
    LaunchPorts,
    register_harness,
)
from meridian.lib.harness.semantics import HarnessSemantics
from meridian.lib.launch.composition import (
    ComposedLaunchContent,
    ProjectedContent,
    ProjectionChannels,
)
from meridian.lib.launch.launch_types import CompositionWarning, ResolvedLaunchSpec
from meridian.lib.safety.policy import (
    EnforcementReport,
    ReportLine,
    ReportStatus,
    complete_report,
)

# A directory with either holds customizations ``agy`` would load: its hooks and MCP servers
# would run beside the control root's, and a second Meridian plugin runs Meridian's hooks twice.
_OWN_CUSTOMIZATIONS = (".agy", ".agents")


def _workspace_roots(spec: ResolvedLaunchSpec) -> tuple[Path, ...]:
    """The roots beyond the working directory the session may use: the workspace's, the
    context directories' and the task directory. Bind records them as the write roots a
    sandbox would protect; the runtime and temp roots it also projects are not among them."""

    roots = tuple(Path(root) for root in spec.permission_policy.sandbox_write_roots)
    return roots[1:]  # the first is the control root, which is where ``agy`` starts


def _split_roots(spec: ResolvedLaunchSpec) -> tuple[list[Path], list[Path]]:
    added: list[Path] = []
    skipped: list[Path] = []
    for root in _workspace_roots(spec):
        has_own = any((root / name).exists() for name in _OWN_CUSTOMIZATIONS)
        (skipped if has_own else added).append(root)
    return added, skipped


class AgyAdapter(AgyReader, BaseHarnessAdapter[ResolvedLaunchSpec]):
    BASE_COMMAND: ClassVar[tuple[str, ...]] = ("agy",)
    PRIMARY_BASE_COMMAND: ClassVar[tuple[str, ...]] = ("agy",)
    _CONSUMED_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"model", "agent", "extra_args", "user_turn_content", "continue_harness_session_id"}
    )

    @property
    def id(self) -> HarnessId:
        return HarnessId.AGY

    @property
    def capabilities(self) -> HarnessCapabilities:
        # A resume names its model with ``--model``; without it ``agy`` falls back to the
        # person's default.
        return HarnessCapabilities(supports_session_resume=True, supports_named_primary_resume=True)

    @property
    def contract(self) -> HarnessContract:
        return HarnessContract(
            capabilities=self.capabilities,
            transport_ids=(),
            launch_spec_cls="ResolvedLaunchSpec",
            bootstrap_mode=BootstrapMode.SUBPROCESS_ONLY,
            workspace_roots=WorkspaceRoots.ADD_DIR_ARGS,
        )

    @property
    def consumed_fields(self) -> frozenset[str]:
        return self._CONSUMED_FIELDS

    @property
    def explicitly_ignored_fields(self) -> frozenset[str]:
        # ``effort`` rides inside the model slug Mars chose; guidance reaches the session
        # through the plugin's hook, not the command.
        return frozenset(SpawnParams.model_fields) - self._CONSUMED_FIELDS

    def resolve_launch_spec(self, run: SpawnParams) -> ResolvedLaunchSpec:
        return ResolvedLaunchSpec(
            harness=HarnessId.AGY,
            model=str(run.model).strip() if run.model else None,
            agent_name=(run.agent or "").strip() or None,
            continue_session_id=(run.continue_harness_session_id or "").strip() or None,
            user_turn_content=(run.user_turn_content or "").strip() or None,
            extra_args=run.extra_args,
            interactive=True,
        )

    def project_content(self, content: ComposedLaunchContent) -> ProjectedContent:
        """Only the person's own prompt: the plugin's hook delivers Meridian's guidance, and
        the options that would add context documents are refused for a printed launch."""

        return ProjectedContent(
            system_prompt="",
            user_turn_content=content.user_task_prompt,
            reference_routing=(),
            channels=ProjectionChannels(
                system_instruction="none", user_task_prompt="user-turn", task_context="inline"
            ),
        )

    def launch_spec_warnings(self, spec: ResolvedLaunchSpec) -> tuple[CompositionWarning, ...]:
        return tuple(
            CompositionWarning(
                code="agy_workspace_root_skipped",
                message=(
                    f"{root} has its own .agy/ or .agents/, so it is not added to the agy "
                    "workspace: its hooks and MCP servers would run beside the project's, "
                    "and Meridian's hooks would run twice. The session can still reach it "
                    "outside its workspace where your agy settings allow."
                ),
                detail={"root": root.as_posix()},
            )
            for root in _split_roots(spec)[1]
        )

    def project_launch_env(
        self,
        *,
        spec: ResolvedLaunchSpec,
        system_prompt_path: Path,
        env: Mapping[str, str],
        primary_mode: bool,
    ) -> dict[str, str]:
        _ = system_prompt_path, env, primary_mode
        overlay: dict[str, str] = {}
        if spec.agent_name:
            overlay[AGENT_ENV] = spec.agent_name
        if spec.permission_policy.approval != "default":
            overlay[APPROVAL_ENV] = spec.permission_policy.approval
        return overlay


def project_agy_spec_to_cli_args(
    spec: ResolvedLaunchSpec, *, base_command: tuple[str, ...]
) -> list[str]:
    command = list(base_command)
    if spec.model:
        command += ["--model", spec.model]
    if spec.agent_name:
        command += ["--agent", spec.agent_name]
    if spec.continue_session_id:
        command += ["--conversation", spec.continue_session_id]
    approval = spec.permission_policy.approval
    if approval == "never":
        command.append("--dangerously-skip-permissions")
    elif approval == "auto":
        command += ["--mode", "auto"]
    for root in _split_roots(spec)[0]:
        command += ["--add-dir", root.as_posix()]
    command.extend(spec.extra_args)
    if spec.user_turn_content:
        command += ["--prompt-interactive", spec.user_turn_content]
    return command


_APPROVAL_LINES: dict[str, tuple[ReportStatus, str]] = {
    "never": ("native", "--dangerously-skip-permissions"),
    "auto": ("native", "--mode auto"),
    "confirm": ("native", "agy asks as it does by default"),
}


def agy_permission_report(spec: ResolvedLaunchSpec) -> EnforcementReport:
    policy = spec.permission_policy
    reported: list[ReportLine] = []
    if policy.approval in _APPROVAL_LINES:
        status, note = _APPROVAL_LINES[policy.approval]
        reported.append(ReportLine(f"approval: {policy.approval}", status, note))
    return complete_report(policy, reported, harness="agy")


register_harness(
    LaunchPorts(
        harness_id=HarnessId.AGY,
        adapter=AgyAdapter(),
        spec_cls=ResolvedLaunchSpec,
        projections=HarnessProjectionPorts(
            subprocess_cli_args=project_agy_spec_to_cli_args,
            permission_report=agy_permission_report,
        ),
        # No events reach Meridian from ``agy``: the plugin writes the row.
        semantics=HarnessSemantics(events={}),
    )
)
