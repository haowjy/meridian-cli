"""Codex's compile of the permission policy.

Codex takes a sandbox mode and an approval policy per launch. Unscoped allow and deny
rules for web search and image viewing compile to Codex's per-launch switches; shell rules
go to the ``PreToolUse`` hook, and Codex has no native carrier for the other tool
rules. The switches read semantic
capabilities, never harness-native names. The projectors spell this typed result for
``codex exec``, ``codex app-server``, and the thread payload.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from meridian.lib.harness.projections.compaction import compaction_report
from meridian.lib.harness.projections.native_config import (
    project_codex_native_config,
)
from meridian.lib.harness.projections.tool_extension_policy import (
    ExtensionPolicy,
    ExtensionRule,
    bash_fails_closed,
    command_grants,
    extension_rules,
    needs_extension,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.policy import (
    CONSTRAINED_SANDBOX_MODES,
    Allow,
    ApprovalMode,
    EffectivePolicy,
    EnforcementReport,
    Hosts,
    NetworkProjection,
    PermissionPolicy,
    ReportLine,
    ReportStatus,
    SandboxMode,
    complete_report,
    network_policy_element,
    platform_element,
    rule_element,
)
from meridian.lib.tools import PLATFORM_ORIGIN, ToolRule

_APPROVAL_POLICY_BY_MODE: dict[ApprovalMode, str | None] = {
    "default": None,
    "auto": "on-request",
    # Codex 0.163 accepts only ``on-request`` and ``never``; ``untrusted`` is rejected
    # at startup, so ``confirm`` is ``on-request`` with a human reviewer (see below).
    "confirm": "on-request",
    "never": "never",
    "yolo": "never",
}

# Who answers an ``on-request`` escalation. Set for every mode that can raise one, so a launch
# never inherits the reviewer from ``~/.codex/config.toml`` or from the thread it forks:
# ``auto`` is Codex's own auto (the guardian reviewer); ``confirm`` is ``user``, which a spawn
# refuses and a primary asks. ``never`` and ``yolo`` raise no request, so no reviewer is read.
# ``codex exec --approve-for-me`` is not used: it refuses ``--sandbox``, forces
# workspace-write, and app-server has no such flag.
_APPROVALS_REVIEWER_BY_MODE: dict[ApprovalMode, str] = {"auto": "auto_review", "confirm": "user"}

_SANDBOX_MODE_BY_MODE: dict[SandboxMode, str | None] = {
    "default": None,
    "read-only": "read-only",
    "workspace-write": "workspace-write",
    "danger-full-access": "danger-full-access",
}


@dataclass(frozen=True)
class CodexNetworkSettings:
    """The Codex config values Meridian owns for one launch."""

    network_access: bool | None = None
    proxy_enabled: bool = False
    domains: tuple[tuple[str, Literal["allow", "deny"]], ...] = ()


@dataclass(frozen=True)
class CodexPermissions:
    """Codex's launch-time permission settings; ``None`` leaves Codex's default.

    ``builtin_agents_disabled``: Codex's own multi-agent tools are invisible to Meridian,
    and v2 tells a lone spawn it is `/root` of a team, so it can message itself and wait
    forever.

    ``tool_hook``: the rules Codex's ``PreToolUse`` hook enforces on its shell tool, for
    shell rules the process-level switches cannot express.
    """

    sandbox_mode: str | None
    network: CodexNetworkSettings
    approval_policy: str | None
    approvals_reviewer: str | None
    web_search: bool | None
    view_image: bool | None
    builtin_agents_disabled: bool
    tool_hook: ExtensionPolicy | None


def _tool_switch(rule: ToolRule) -> tuple[str, bool, str] | None:
    """Return the Codex switch an exact, unscoped tool rule controls."""

    if rule.pattern is not None or rule.action == "ask":
        return None
    enabled = rule.action == "allow"
    if rule.capability in {"web", "web_search"}:
        note = "-c tools.web_search=true" if enabled else '-c web_search="disabled"'
        return "web_search", enabled, note
    if rule.capability == "image":
        verb = "--enable" if enabled else "--disable"
        return "view_image", enabled, f"{verb} view_image"
    return None


def project_codex_network(
    policy: PermissionPolicy,
) -> NetworkProjection[CodexNetworkSettings]:
    """Project Meridian-owned policy inputs onto Codex network config."""

    if policy.sandbox == "read-only":
        return NetworkProjection(
            native=CodexNetworkSettings(),
            effective=Hosts(),
        )
    if policy.sandbox != "workspace-write":
        return NetworkProjection(
            native=CodexNetworkSettings(),
            effective=Allow(),
        )
    network = policy.network
    if isinstance(network, Allow) and not network.deny:
        return NetworkProjection(
            native=CodexNetworkSettings(network_access=True),
            effective=network,
        )
    if isinstance(network, Hosts) and not network.allow:
        return NetworkProjection(
            native=CodexNetworkSettings(network_access=False),
            effective=network,
        )

    domains: dict[str, Literal["allow", "deny"]] = {}
    if isinstance(network, Allow):
        domains["*"] = "allow"
    else:
        domains.update((host, "allow") for host in sorted(network.allow))
    domains.update((host, "deny") for host in sorted(network.deny))
    return NetworkProjection(
        native=CodexNetworkSettings(
            network_access=True,
            proxy_enabled=True,
            domains=tuple(domains.items()),
        ),
        effective=network,
    )


def effective_codex_policy(
    policy: PermissionPolicy,
    *,
    native_config: Mapping[str, object],
) -> EffectivePolicy:
    """Return the sandbox and network Codex enforces from Meridian-owned inputs."""

    del native_config

    return EffectivePolicy(
        sandbox=policy.sandbox,
        network=project_codex_network(policy).effective,
    )


def _hook_carries(rule: ToolRule | ExtensionRule) -> bool:
    """Whether the hook can carry this rule: a shell rule, except a scoped ``ask``."""

    return rule.capability in {"bash", "*"} and not (rule.action == "ask" and rule.pattern)


def _compile_tool_hook(policy: PermissionPolicy) -> ExtensionPolicy | None:
    """The shell rules Codex's ``PreToolUse`` hook enforces, when the shell switch would lose some.

    The hook sees one shell tool, so only ``bash`` and ``*`` rules go in; Mars names the tool
    ``exec_command`` for Codex, which the enforcer knows as ``bash``. A hook cannot ask, so
    an unscoped ``ask`` is carried as ``deny``: carrying nothing would let the command run.
    A scoped ``ask`` is not carried and stays ``unsupported``.

    Codex has no shell switch worth using: ``features.shell_tool=false`` would also disable
    Meridian's own commands. So every shell rule that restricts goes to the hook: an unscoped
    ``bash`` deny or ask, or a scoped rule beside something that restricts. A ``*: deny``
    allowlist alone stays off the hook, since the hook would also block file reads.
    """

    carried = [rule for rule in extension_rules(policy.rules) if _hook_carries(rule)]
    commands = command_grants(policy)
    shell_restricted = any(
        rule.capability == "bash" and rule.pattern is None and rule.action != "allow"
        for rule in carried
    )
    scoped = any(rule.pattern is not None for rule in carried)
    if not (
        shell_restricted or (scoped and needs_extension(tuple(carried), meridian_commands=commands))
    ):
        return None
    rules: list[ExtensionRule] = []
    for rule in carried:
        if rule.action == "ask":
            rule = rule.model_copy(update={"action": "deny", "label": f"{rule.label} (ask)"})
        if rule.capability == "bash":
            rule = rule.model_copy(update={"tool": "bash"})
        rules.append(rule)
    return ExtensionPolicy(
        rules=tuple(rules),
        meridian_commands=commands,
        bash_fail_closed=bash_fails_closed(tuple(rules)),
    )


def _compile_codex_permission_values(
    spec: ResolvedLaunchSpec,
) -> tuple[CodexPermissions, NetworkProjection[CodexNetworkSettings]]:
    """Resolve every native permission value, once."""

    policy = spec.permission_policy
    # Under an allowlist, search and image viewing are off unless a rule allows them. The
    # shell stays on because Codex reads files through it.
    off = False if policy.is_allowlist else None
    switches: dict[str, bool | None] = {
        "web_search": off,
        "view_image": off,
    }
    # `web_search` is narrower than `web`, so it decides the search switch whatever the order.
    for rule in sorted(policy.rules, key=lambda rule: rule.capability == "web_search"):
        switch = _tool_switch(rule)
        if switch is not None:
            switches[switch[0]] = switch[1]

    tool_hook = _compile_tool_hook(policy)
    network = project_codex_network(policy)
    return (
        CodexPermissions(
            sandbox_mode=_SANDBOX_MODE_BY_MODE[policy.sandbox],
            network=network.native,
            approval_policy=_APPROVAL_POLICY_BY_MODE[policy.approval],
            approvals_reviewer=_APPROVALS_REVIEWER_BY_MODE.get(policy.approval),
            web_search=switches["web_search"],
            view_image=switches["view_image"],
            builtin_agents_disabled=any(
                rule.kind == "builtin-agents" for rule in policy.platform_rules
            ),
            tool_hook=tool_hook,
        ),
        network,
    )


_HOOK_NOTE = "PreToolUse hook (the launch is refused unless Codex lists it as intact and trusted)"
# What the hook is: wildcard rules on each command segment, a guard against accidents.
_HOOK_LIMITS = (
    "it matches each `;`/`&&`/`||`/`|`/`&`/newline segment against the rules, so wrapped or "
    "compound forms it does not see (`sudo git reset --hard`, `bash -c`, substitutions, "
    "subshells, scripts, interpreters, `git -C <dir> reset --hard`), `apply_patch` and MCP "
    "calls get past it, and an agent that can replace the Python interpreter turns it off"
)


def _hook_report_line(rule: ToolRule) -> ReportLine:
    element = rule_element(rule)
    if rule.action == "allow":
        return ReportLine(element, "native", f"{_HOOK_NOTE}; a matching command may run")
    if rule.pattern is None:
        asked = " Codex's hook cannot ask, so `ask` is a deny;" if rule.action == "ask" else ""
        agents = (
            " Web search and image viewing are off unless allowed."
            if rule.capability == "*"
            else ""
        )
        return ReportLine(
            element,
            "partial",
            f"{_HOOK_NOTE} refuses every shell command segment no scoped allow matches, and "
            f"shell syntax such as expansion and redirection.{asked} Reads are bound too: Codex "
            "reads files through the shell, so allow the read commands the agent needs. Beyond "
            f"the allowed commands themselves (a permitted interpreter can run anything): "
            f"{_HOOK_LIMITS}.{agents}",
        )
    return ReportLine(
        element,
        "partial",
        f"{_HOOK_NOTE}; a guard against accidents, not a sandbox: {_HOOK_LIMITS}",
    )


def _codex_report(
    spec: ResolvedLaunchSpec,
    policy: PermissionPolicy,
    permissions: CodexPermissions,
    network: NetworkProjection[CodexNetworkSettings],
    native_config_report: EnforcementReport,
) -> EnforcementReport:
    lines: list[ReportLine] = []
    for rule in policy.rules:
        switch = _tool_switch(rule)
        if (
            rule.origin == PLATFORM_ORIGIN
            and rule.capability == "agent"
            and permissions.builtin_agents_disabled
        ):
            lines.append(
                ReportLine(
                    rule_element(rule),
                    "platform",
                    "-c features.multi_agent_v2=false -c agents.max_depth=0",
                )
            )
        elif (
            permissions.tool_hook is not None
            and _hook_carries(rule)
            and (rule.pattern is not None or rule.action != "allow")
        ):
            lines.append(_hook_report_line(rule))
        elif rule.capability == "bash" and rule.pattern is None and rule.action == "allow":
            lines.append(ReportLine(rule_element(rule), "native", "Codex's shell is on by default"))
        elif switch is not None and getattr(permissions, switch[0]) != switch[1]:
            lines.append(ReportLine(rule_element(rule), "partial", "overridden by a narrower rule"))
        elif switch is not None:
            lines.append(ReportLine(rule_element(rule), "native", switch[2]))
        elif rule.pattern is not None and rule.capability in {"bash", "web", "web_search", "image"}:
            lines.append(
                ReportLine(
                    rule_element(rule),
                    "unsupported",
                    "Codex can't pre-approve a scoped command; it follows approval_policy",
                )
            )
        elif rule.action == "ask":
            lines.append(
                ReportLine(
                    rule_element(rule),
                    "unsupported",
                    "Codex has no per-launch tool approval rule",
                )
            )
        elif rule.capability == "*" and rule.action == "deny" and rule.pattern is None:
            lines.append(
                ReportLine(
                    rule_element(rule),
                    "partial",
                    "web search and image viewing off unless allowed; the shell stays on, "
                    "since Codex reads files through it",
                )
            )
    for platform in policy.platform_rules:
        if platform.kind == "builtin-agents":
            lines.append(
                ReportLine(
                    platform_element(platform),
                    "platform",
                    "-c features.multi_agent_v2=false -c agents.max_depth=0",
                )
            )
        elif platform.kind == "meridian-commands":
            commands = ", ".join(
                f"{command.match} {command.command!r}" for command in platform.commands
            )
            rules_error = spec.codex_runtime.rules_file_error
            rules_path = spec.codex_runtime.rules_file_path
            constrained_sandbox = policy.sandbox in CONSTRAINED_SANDBOX_MODES
            spawn_escape = constrained_sandbox and policy.has_spawn_capability
            if spawn_escape and rules_error is not None:
                note = (
                    "Meridian could not write its Codex rules file, so commands remain subject "
                    f"to the sandbox ({rules_error}); requested matches: {commands}"
                )
            elif spawn_escape and rules_path is None:
                note = (
                    "a real launch would write $CODEX_HOME/rules/meridian.rules so "
                    "`meridian spawn` can run outside Codex's sandbox; other requested "
                    f"matches remain ordinary shell calls ({commands})"
                )
            elif spawn_escape:
                note = (
                    "the Meridian-owned user rules file lets `meridian spawn` run outside "
                    "Codex's sandbox for every Codex session of this user; other requested "
                    f"matches remain ordinary shell calls ({commands})"
                )
            else:
                note = (
                    f"Codex cannot grant shell command matches ({commands}); they run as "
                    "ordinary shell calls, subject to its sandbox and approval policy"
                )
            status: ReportStatus = (
                "partial" if spawn_escape and rules_error is None else "unsupported"
            )
            if permissions.tool_hook is not None:
                note += "; the shell hook lets them past the profile's shell rules"
                status = "platform" if status == "unsupported" else status
            lines.append(
                ReportLine(
                    platform_element(platform),
                    status,
                    note,
                )
            )
    if permissions.sandbox_mode is not None:
        sandbox_note = f"sandbox_mode={permissions.sandbox_mode}"
        sandbox_status = "native"
        if policy.sandbox in CONSTRAINED_SANDBOX_MODES and policy.has_spawn_capability:
            rules_error = spec.codex_runtime.rules_file_error
            rules_path = spec.codex_runtime.rules_file_path
            if rules_error is not None:
                sandbox_status = "partial"
                sandbox_note += (
                    "; Meridian could not write "
                    f"{rules_path or '$CODEX_HOME/rules/meridian.rules'}, so `meridian spawn` "
                    f"remains inside Codex's sandbox: {rules_error}"
                )
            elif rules_path is None:
                sandbox_note += (
                    "; a real launch would write $CODEX_HOME/rules/meridian.rules so "
                    "`meridian spawn` can run outside Codex's sandbox"
                )
            else:
                sandbox_note += (
                    "; `meridian spawn` runs outside Codex's sandbox via "
                    f"{rules_path}, for every Codex session of this user"
                )
        lines.append(
            ReportLine(
                f"sandbox: {policy.sandbox}",
                sandbox_status,
                sandbox_note,
            )
        )
    network_element = network_policy_element(policy.network)

    if policy.sandbox == "workspace-write":
        if network.native.proxy_enabled:
            lines.append(
                ReportLine(
                    network_element,
                    "partial",
                    (
                        "-c sandbox_workspace_write.network_access=true; "
                        "-c features.network_proxy.enabled=true; "
                        "-c features.network_proxy.domains=<inline map from Meridian's "
                        "host sets>; "
                        "Codex still permits local services"
                    ),
                )
            )
        elif isinstance(network.effective, Allow):
            lines.append(
                ReportLine(
                    network_element,
                    "partial" if network.effective != policy.network else "native",
                    "-c sandbox_workspace_write.network_access=true",
                )
            )
        else:
            rules_error = spec.codex_runtime.rules_file_error
            escaped = rules_error is None
            lines.append(
                ReportLine(
                    network_element,
                    "partial" if escaped or network.effective != policy.network else "native",
                    (
                        "-c sandbox_workspace_write.network_access=false"
                        + (
                            "; the Meridian-owned user rule still lets `meridian spawn` run "
                            "outside the sandbox with network access"
                            if escaped
                            else ""
                        )
                    ),
                )
            )
    elif policy.sandbox == "read-only":
        if (
            network.effective != policy.network
            or isinstance(policy.network, Allow)
            or policy.network.allow
        ):
            lines.append(
                ReportLine(
                    network_element,
                    "unsupported",
                    "Codex read-only has no network-enabled mode",
                )
            )
        else:
            lines.append(
                ReportLine(
                    network_element,
                    "native",
                    "Codex read-only keeps shell network and sockets off",
                )
            )
    elif policy.sandbox == "danger-full-access":
        enforced = network.effective == policy.network
        lines.append(
            ReportLine(
                network_element,
                "native" if enforced else "unsupported",
                (
                    "danger-full-access leaves shell network unrestricted"
                    if enforced
                    else "not enforced under danger-full-access; shell network remains available"
                ),
            )
        )
    else:
        enforced = network.effective == policy.network
        lines.append(
            ReportLine(
                network_element,
                "partial" if enforced else "unsupported",
                ("sandbox is left to Codex, so Meridian does not project network_access"),
            )
        )
    if permissions.approval_policy is not None:
        note = f"approval_policy={permissions.approval_policy}"
        if permissions.approvals_reviewer is not None:
            note += f", approvals_reviewer={permissions.approvals_reviewer}"
            if permissions.approvals_reviewer == "auto_review":
                note += ": Codex's reviewer decides escalations and can approve one"
        if policy.approval == "confirm":
            lines.append(
                ReportLine(
                    f"approval: {policy.approval}",
                    "partial",
                    f"{note}; Codex has no mode stricter than on-request, so a request the "
                    "sandbox allows to escalate is asked of the user (a spawn refuses it)",
                )
            )
        else:
            lines.append(ReportLine(f"approval: {policy.approval}", "native", note))
    lines.extend(native_config_report)
    lines.extend(compaction_report(spec, harness="Codex"))
    return complete_report(
        policy,
        lines,
        harness="Codex",
    )


def compile_codex_permissions(
    spec: ResolvedLaunchSpec,
) -> tuple[CodexPermissions, EnforcementReport]:
    """Compile ``spec.permission_policy`` for Codex, with the report of what it did."""

    policy = spec.permission_policy
    permissions, network = _compile_codex_permission_values(spec)
    _, native_config_report = project_codex_native_config(spec.native_config)
    return permissions, _codex_report(
        spec,
        policy,
        permissions,
        network,
        native_config_report,
    )


__all__ = [
    "CodexPermissions",
    "compile_codex_permissions",
    "effective_codex_policy",
    "project_codex_network",
]
