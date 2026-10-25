"""Claude shell-network projection and enforcement reporting."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass

from meridian.lib.safety.policy import (
    Allow,
    Host,
    NetworkPolicy,
    NetworkProjection,
    PermissionPolicy,
    ReportLine,
    network_allows_local_services,
    network_policy_element,
)

CLAUDE_NETWORK_ALLOW_DOMAINS = ("*", "localhost", "127.0.0.1")


@dataclass(frozen=True)
class ClaudeNetworkSettings:
    """The Claude ``sandbox.network`` settings Meridian owns for one launch."""

    strict_allowlist: bool = False
    allowed_domains: tuple[str, ...] = ()
    denied_domains: tuple[str, ...] = ()
    allow_local_binding: bool = False
    allow_all_unix_sockets: bool = False


def _claude_domains(hosts: frozenset[Host]) -> tuple[str, ...]:
    """Translate apex-plus-subdomain wildcards to Claude's two entries."""

    domains: list[str] = []
    for host in sorted(hosts):
        if host.startswith("**."):
            apex = host.removeprefix("**.")
            domains.extend((apex, f"*.{apex}"))
        else:
            domains.append(host)
    return tuple(dict.fromkeys(domains))


def project_claude_network(
    policy: PermissionPolicy,
    *,
    native_config: Mapping[str, object] | None = None,
) -> NetworkProjection[ClaudeNetworkSettings | None]:
    """Project only Meridian's resolved policy onto Claude network settings."""

    _ = native_config
    if policy.sandbox not in {"read-only", "workspace-write"}:
        return NetworkProjection(native=None, effective=Allow())

    network = policy.network
    if isinstance(network, Allow):
        return NetworkProjection(
            native=ClaudeNetworkSettings(
                allowed_domains=CLAUDE_NETWORK_ALLOW_DOMAINS,
                denied_domains=_claude_domains(network.deny),
                allow_local_binding=True,
                allow_all_unix_sockets=True,
            ),
            effective=network,
        )

    allow_local_services = network_allows_local_services(network)
    return NetworkProjection(
        native=ClaudeNetworkSettings(
            strict_allowlist=True,
            allowed_domains=_claude_domains(network.allow),
            denied_domains=_claude_domains(network.deny),
            allow_local_binding=allow_local_services,
            allow_all_unix_sockets=allow_local_services,
        ),
        effective=network,
    )


def effective_claude_network(
    policy: PermissionPolicy,
    *,
    native_config: Mapping[str, object],
) -> NetworkPolicy:
    """Return the network policy Claude enforces from Meridian-owned inputs."""

    return project_claude_network(policy, native_config=native_config).effective


def claude_network_line(
    policy: PermissionPolicy,
    projection: NetworkProjection[ClaudeNetworkSettings | None],
    *,
    sandbox_enabled: bool,
    excluded_commands: tuple[str, ...],
) -> ReportLine:
    """Report Claude's projection of Meridian's network policy."""

    element = network_policy_element(policy.network)
    if isinstance(policy.network, Allow):
        if projection.native is None:
            return ReportLine(
                element,
                "native" if projection.effective == policy.network else "unsupported",
                "no Claude Bash sandbox is projected; shell network remains available",
            )
        assert sandbox_enabled
        notes = [
            "--settings sandbox.network.allowedDomains allows all destinations, "
            "sandbox.network.allowLocalBinding=true, "
            "sandbox.network.allowAllUnixSockets=true"
        ]
        if projection.native.denied_domains:
            notes.append("deniedDomains projects the deny set above")
        if sys.platform.startswith("linux"):
            notes.append("host localhost remains mediated by Claude's proxy on Linux")
        return ReportLine(
            element,
            "partial" if sys.platform.startswith("linux") else "native",
            "; ".join(notes),
        )

    if projection.native is None:
        return ReportLine(
            element,
            "unsupported",
            "no Claude Bash sandbox is projected; shell network remains available",
        )
    assert sandbox_enabled
    notes = ["--settings sandbox.network.strictAllowlist=true"]
    if projection.native.allowed_domains:
        notes.append("allowedDomains projects the allow set above")
    if projection.native.denied_domains:
        notes.append("deniedDomains projects the deny set above")
    if any(host.startswith("**.") for host in (*policy.network.allow, *policy.network.deny)):
        notes.append("each **.x entry is emitted as x plus *.x for Claude")
    local_services = projection.native.allow_local_binding
    if local_services:
        notes.append(
            "localhost or 127.0.0.1 opts into Claude's coarse "
            "allowLocalBinding=true and allowAllUnixSockets=true switches"
        )
    else:
        notes.append("allowLocalBinding=false and allowAllUnixSockets=false")
    if sys.platform.startswith("linux"):
        notes.append(
            "processes in the same sandbox can still connect over its private TCP "
            "loopback, so deny is partial on Linux"
        )
    if excluded_commands:
        notes.append(
            "excludedCommands run outside the sandbox and can still use the network: "
            + ",".join(excluded_commands)
        )
    return ReportLine(
        element,
        "partial"
        if sys.platform.startswith("linux") or local_services or excluded_commands
        else "native",
        "; ".join(notes),
    )


__all__ = [
    "CLAUDE_NETWORK_ALLOW_DOMAINS",
    "ClaudeNetworkSettings",
    "claude_network_line",
    "effective_claude_network",
    "project_claude_network",
]
