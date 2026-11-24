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
from meridian.lib.harness.agy.permissions import AgyPermissions, compile_agy_permissions
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
    join_content_blocks,
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
    # Resolved once at bind (``bind_agy_spec``): the one ``--settings`` value and the report
    # lines for it. ``None`` on a spec that did not pass through bind.
    permissions: AgyPermissions | None = None


def bind_agy_spec(
    spec: ResolvedLaunchSpec, env: Mapping[str, str], child_cwd: Path
) -> AgyLaunchSpec:
    """Compile the agent's rules and the person's own settings once for argv and the report."""

    assert isinstance(spec, AgyLaunchSpec)
    compiled = compile_agy_permissions(spec, child_cwd)
    return spec.model_copy(update={"permissions": compiled, "extra_args": compiled.extra_args})


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
        """The --from handoff and the person's prompt, in one ``--prompt-interactive`` value.

        The plugin's hook delivers Meridian's guidance. The options that add reference
        documents are refused for a printed launch, so nothing else joins the prompt.
        """

        prior_output = content.prior_output.strip()
        user_task_prompt = content.user_task_prompt.strip()
        message = (
            f"## User message\n{user_task_prompt}"
            if prior_output and user_task_prompt
            else user_task_prompt
        )
        user_turn = join_content_blocks(prior_output, message)
        return ProjectedContent(
            system_prompt="",
            user_turn_content=user_turn,
            reference_routing=(),
            channels=ProjectionChannels(
                system_instruction="none", user_task_prompt="user-turn", task_context="user-turn"
            ),
        )

    def launch_spec_warnings(self, spec: ResolvedLaunchSpec) -> tuple[CompositionWarning, ...]:
        skipped = _split_roots(spec)[1]
        if not skipped:
            return ()
        roots = [root.as_posix() for root in skipped]
        return (
            CompositionWarning(
                code="agy_workspace_root_skipped",
                message=(
                    f"Skipped from the agy workspace (own .agy/ or .agents/): {', '.join(roots)}. "
                    "Their hooks and MCP servers do not load; the directories stay reachable "
                    "by absolute path."
                ),
            ),
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
    assert isinstance(spec, AgyLaunchSpec)
    if spec.permissions is not None and spec.permissions.settings:
        command += ["--settings", spec.permissions.settings]
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
    assert isinstance(spec, AgyLaunchSpec)
    if spec.permissions is not None:
        reported.extend(spec.permissions.lines)
    return complete_report(policy, reported, harness="agy")


# What the person's options would change, and why it cannot reach a session Meridian does not
# start: the plugin's hook and server see the conversation only after the person runs the
# command, and a command line has no place for these.
_FORK_REASON = "a fork starts a conversation Meridian would have to create"
_REFERENCE_REASON = "reference files are not passed to the printed command"
_NO_NETWORK_REASON = "agy has no network flag Meridian can set"
_NO_COMPACTION_REASON = "agy has no compaction flag"

_PRINTED = PrintedLaunchPorts(
    refused_options={
        "--fork": _FORK_REASON,
        "--fork-fresh": _FORK_REASON,
        "-f": _REFERENCE_REASON,
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
            bind_spec=bind_agy_spec,
        ),
        # No events reach Meridian from ``agy``: the plugin writes the row.
        semantics=HarnessSemantics(events={}),
        printed=_PRINTED,
    )
)
