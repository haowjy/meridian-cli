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
from meridian.lib.harness.agy.permissions import compile_agy_permissions
from meridian.lib.harness.agy.state import (
    AGENT_ENV,
    APPROVAL_ENV,
    PROJECT_ENV,
    launch_script_dir,
)
from meridian.lib.harness.agy.transcript import AgyReader
from meridian.lib.harness.bundle import (
    HarnessProjectionPorts,
    LaunchPorts,
    PrintedLaunchPorts,
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


class AgyLaunchSpec(ResolvedLaunchSpec):
    # The roots beyond the working directory the session may use (``SpawnParams``): the
    # command adds them with ``--add-dir``.
    workspace_roots: tuple[Path, ...] = ()


def _split_roots(spec: ResolvedLaunchSpec) -> tuple[list[Path], list[Path]]:
    assert isinstance(spec, AgyLaunchSpec)  # the only spec this adapter resolves
    added: list[Path] = []
    skipped: list[Path] = []
    for root in spec.workspace_roots:
        has_own = any((root / name).exists() for name in _OWN_CUSTOMIZATIONS)
        (skipped if has_own else added).append(root)
    return added, skipped


class AgyAdapter(AgyReader, BaseHarnessAdapter[ResolvedLaunchSpec]):
    BASE_COMMAND: ClassVar[tuple[str, ...]] = ("agy",)
    PRIMARY_BASE_COMMAND: ClassVar[tuple[str, ...]] = ("agy",)
    _CONSUMED_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "model",
            "agent",
            "extra_args",
            "user_turn_content",
            "continue_harness_session_id",
            "workspace_roots",
        }
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
            launch_spec_cls="AgyLaunchSpec",
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

    def resolve_launch_spec(self, run: SpawnParams) -> AgyLaunchSpec:
        return AgyLaunchSpec(
            harness=HarnessId.AGY,
            model=str(run.model).strip() if run.model else None,
            agent_name=(run.agent or "").strip() or None,
            continue_session_id=(run.continue_harness_session_id or "").strip() or None,
            user_turn_content=(run.user_turn_content or "").strip() or None,
            extra_args=run.extra_args,
            workspace_roots=run.workspace_roots,
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
        _ = system_prompt_path, primary_mode
        overlay: dict[str, str] = {}
        if project := env.get("MERIDIAN_PROJECT_DIR"):
            overlay[PROJECT_ENV] = project
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
    # agy ignores --agent on a resumed conversation (and warns); the conversation keeps its
    # agent, and the row keeps it through the env.
    if spec.agent_name and not spec.continue_session_id:
        command += ["--agent", spec.agent_name]
    if spec.continue_session_id:
        command += ["--conversation", spec.continue_session_id]
    approval = spec.permission_policy.approval
    if approval == "never":
        command.append("--dangerously-skip-permissions")
    elif approval == "auto":
        command += ["--mode", "auto"]
    if settings := compile_agy_permissions(spec)[0]:
        command += ["--settings", settings]
    for root in _split_roots(spec)[0]:
        command += ["--add-dir", root.as_posix()]
    command.extend(spec.extra_args)
    if spec.user_turn_content:
        command += ["--prompt-interactive", spec.user_turn_content]
    return command


_APPROVAL_LINES: dict[str, tuple[ReportStatus, str]] = {
    "never": ("native", "--dangerously-skip-permissions"),
    "auto": ("native", "--mode auto"),
}


def agy_permission_report(spec: ResolvedLaunchSpec) -> EnforcementReport:
    policy = spec.permission_policy
    reported: list[ReportLine] = []
    if policy.approval in _APPROVAL_LINES:
        status, note = _APPROVAL_LINES[policy.approval]
        reported.append(ReportLine(f"approval: {policy.approval}", status, note))
    reported.extend(compile_agy_permissions(spec)[1])
    return complete_report(policy, reported, harness="agy")


# What the person's options would change, and why it cannot reach a session Meridian does not
# start: the plugin's hook and server see the conversation only after the person runs the
# command, and a command line has no place for these.
_FORK_REASON = "a fork starts a conversation Meridian would have to create"
_PROMPT_REASON = "the printed command carries only your own prompt"
_NO_NETWORK_REASON = "agy has no network flag Meridian can set"
_NO_COMPACTION_REASON = "agy has no compaction flag"

_PRINTED = PrintedLaunchPorts(
    refused_options={
        "--fork": _FORK_REASON,
        "--fork-fresh": _FORK_REASON,
        "--from": _PROMPT_REASON,
        "-f": _PROMPT_REASON,
        "--skills": "agy loads skills from the project's synced plugin, not from the command",
        "--goal": "a goal is a completion contract Meridian's runner enforces",
        "--sandbox": "agy has no sandbox flag Meridian can set",
        "--network": _NO_NETWORK_REASON,
        "--network-allow": _NO_NETWORK_REASON,
        "--network-deny": _NO_NETWORK_REASON,
        "--autocompact": _NO_COMPACTION_REASON,
        "--autocompact-pct": _NO_COMPACTION_REASON,
        "--timeout": "Meridian does not run agy, so it cannot stop it",
        "--approval confirm": "no agy --mode asks before every edit (accept-edits, auto, plan)",
    },
    refused_passthrough={
        "--append-system-prompt": "agy has no system-prompt flag to carry it",
    },
    workspace_note=(
        "--work and --task-dir do not move the session: it starts in the project directory "
        "and is not attached to the work item."
    ),
    script_dir=launch_script_dir,
)

register_harness(
    LaunchPorts[ResolvedLaunchSpec](
        harness_id=HarnessId.AGY,
        adapter=AgyAdapter(),
        spec_cls=AgyLaunchSpec,
        projections=HarnessProjectionPorts(
            subprocess_cli_args=project_agy_spec_to_cli_args,
            permission_report=agy_permission_report,
        ),
        # No events reach Meridian from ``agy``: the plugin writes the row.
        semantics=HarnessSemantics(events={}),
        printed=_PRINTED,
    )
)
