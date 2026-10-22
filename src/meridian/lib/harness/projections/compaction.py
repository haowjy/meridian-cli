"""Harness-native projection of semantic autocompaction thresholds."""

from __future__ import annotations

from meridian.lib.harness.projections.native_config import (
    CODEX_CONFIG_AUTO_COMPACT_TOKEN_LIMIT_KEY,
    CODEX_CONFIG_POST_TURN_COMPACT_THRESHOLD_PERCENT_KEY,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.policy import EnforcementReport, ReportLine

CLAUDE_AUTOCOMPACT_MIN_TOKENS = 100_000
CLAUDE_AUTOCOMPACT_MAX_TOKENS = 1_000_000

def project_claude_autocompact_args(autocompact: int | None) -> tuple[str, ...]:
    """Return Claude's absolute-token threshold flag when its CLI can express it."""

    if autocompact is None or not (
        CLAUDE_AUTOCOMPACT_MIN_TOKENS
        <= autocompact
        <= CLAUDE_AUTOCOMPACT_MAX_TOKENS
    ):
        return ()
    return ("--autocompact", str(autocompact))


def project_codex_autocompact_args(
    autocompact: int | None, autocompact_pct: int | None
) -> tuple[str, ...]:
    """Return Codex config overrides for the two semantic thresholds."""

    arguments: list[str] = []
    if autocompact is not None:
        arguments.extend(
            ("-c", f"{CODEX_CONFIG_AUTO_COMPACT_TOKEN_LIMIT_KEY}={autocompact}")
        )
    if autocompact_pct is not None:
        arguments.extend(
            (
                "-c",
                f"{CODEX_CONFIG_POST_TURN_COMPACT_THRESHOLD_PERCENT_KEY}="
                f"{autocompact_pct}",
            )
        )
    return tuple(arguments)


def compaction_report(spec: ResolvedLaunchSpec, *, harness: str) -> EnforcementReport:
    """Describe how one harness applies or declines the resolved thresholds."""

    lines: list[ReportLine] = []
    if harness == "Claude":
        if spec.autocompact is not None:
            arguments = project_claude_autocompact_args(spec.autocompact)
            if arguments:
                lines.append(
                    ReportLine(
                        f"autocompact: {spec.autocompact}",
                        "native",
                        f"--autocompact {spec.autocompact}",
                    )
                )
            else:
                lines.append(
                    ReportLine(
                        f"autocompact: {spec.autocompact}",
                        "unsupported",
                        "Claude accepts absolute thresholds from 100000 to 1000000 tokens",
                    )
                )
        if spec.autocompact_pct is not None:
            lines.append(
                ReportLine(
                    f"autocompact_pct: {spec.autocompact_pct}",
                    "native",
                    f"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE={spec.autocompact_pct}",
                )
            )
        return tuple(lines)

    if harness == "Codex":
        if spec.autocompact is not None:
            lines.append(
                ReportLine(
                    f"autocompact: {spec.autocompact}",
                    "native",
                    f"-c {CODEX_CONFIG_AUTO_COMPACT_TOKEN_LIMIT_KEY}={spec.autocompact}",
                )
            )
        if spec.autocompact_pct is not None:
            lines.append(
                ReportLine(
                    f"autocompact_pct: {spec.autocompact_pct}",
                    "native",
                    f"-c {CODEX_CONFIG_POST_TURN_COMPACT_THRESHOLD_PERCENT_KEY}="
                    f"{spec.autocompact_pct}",
                )
            )
        return tuple(lines)

    if spec.autocompact is not None:
        lines.append(
            ReportLine(
                f"autocompact: {spec.autocompact}",
                "unsupported",
                f"not applied for {harness}",
            )
        )
    if spec.autocompact_pct is not None:
        lines.append(
            ReportLine(
                f"autocompact_pct: {spec.autocompact_pct}",
                "unsupported",
                f"not applied for {harness}",
            )
        )
    return tuple(lines)


__all__ = [
    "CLAUDE_AUTOCOMPACT_MAX_TOKENS",
    "CLAUDE_AUTOCOMPACT_MIN_TOKENS",
    "compaction_report",
    "project_claude_autocompact_args",
    "project_codex_autocompact_args",
]
